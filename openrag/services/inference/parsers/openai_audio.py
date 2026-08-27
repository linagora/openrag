"""OpenAI-compatible audio transcription client.

Pipeline:

1. Materialize ``Document.raw_bytes`` to a temporary file via
   :meth:`Document.as_temporary_file`.
2. If the file's suffix is in ``direct_upload_suffixes``, send it to the
   transcription endpoint as-is. Otherwise, decode through
   ``pydub.AudioSegment`` and re-encode as WAV (libsndfile-friendly).
3. Optionally resolve a caller-provided transcription prompt and run a
   caller-provided language detector against the
   prepared file (its result is forwarded to the OpenAI ``language``
   param). The detector is a plain async callable so this client stays
   free of Ray / model-loader coupling — the wiring layer can plug in a
   Whisper actor or any other implementation.
4. Send the file to ``audio.transcriptions.create`` and emit a single
   :class:`TextBlock` with the resulting transcript.

Adapted from the legacy
``components/indexer/loaders/audio/openai.py`` ``AudioTranscriber``;
the new version drops the old in-memory semaphore helper (now
per-instance via ``concurrency_limit``) and the embedded WhisperActor
ref-getter (now an injected callable).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from core.config.model_endpoints import STT_LANGUAGE_KEY, ModelEndpointConfig
from core.indexing.parsers.document_parser import BaseClientParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from openai import AsyncOpenAI
from pydub import AudioSegment

logger = logging.getLogger(__name__)


# Suffixes the transcription backend can ingest as-is, avoiding the ~10x
# size inflation from WAV conversion (Scaleway cap: 100 MB; OpenAI: 25 MB).
_DEFAULT_DIRECT_UPLOAD_SUFFIXES: tuple[str, ...] = (".mp3", ".m4a", ".ogg", ".webm", ".wav")

LanguageDetector = Callable[[Path], Awaitable[str | None]]
TranscriptionPromptResolver = Callable[[], Awaitable[str | None]]
TranscriptionEndpointResolver = Callable[[], ModelEndpointConfig | None]


class OpenAIAudioClient(BaseClientParser):
    """OpenAI-compatible audio transcription client."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        direct_upload_suffixes: Iterable[str] = _DEFAULT_DIRECT_UPLOAD_SUFFIXES,
        language_detector: LanguageDetector | None = None,
        transcription_prompt_resolver: TranscriptionPromptResolver | None = None,
        transcription_endpoint_resolver: TranscriptionEndpointResolver | None = None,
        concurrency_limit: int = 1,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model
        self._direct_upload_suffixes = {s.lower() for s in direct_upload_suffixes}
        self._language_detector = language_detector
        self._transcription_prompt_resolver = transcription_prompt_resolver
        self._transcription_endpoint_resolver = transcription_endpoint_resolver
        self._semaphore = asyncio.Semaphore(max(1, concurrency_limit))
        # Config changes are rare, while transcription requests can be long.
        # Keep one client/limiter per live endpoint configuration so a save in
        # the Admin UI affects the next file without interrupting an in-flight
        # request that still uses the previous connection.
        self._endpoint_clients: dict[tuple[str, str, float], AsyncOpenAI] = {}
        self._endpoint_semaphores: dict[tuple[str, str, int], asyncio.Semaphore] = {}

    def supported_types(self) -> list[str]:
        return [DocumentType.AUDIO.value, DocumentType.VIDEO.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        start = time.time()
        try:
            async with document.as_temporary_file() as input_path:
                endpoint_config = self._resolve_transcription_endpoint()
                async with self._semaphore_for_endpoint(endpoint_config):
                    upload_path, cleanup = await self._prepare_upload(input_path)
                    try:
                        language = self._language_hint(endpoint_config)
                        if language is None and self._language_detector is not None:
                            try:
                                language = await self._language_detector(upload_path)
                            except Exception as exc:
                                logger.warning("Language detection failed: %s", exc)
                        prompt = await self._resolve_prompt()
                        text = await self._transcribe(
                            upload_path,
                            language=language,
                            prompt=prompt,
                            endpoint_config=endpoint_config,
                        )
                    finally:
                        if cleanup:
                            await asyncio.to_thread(upload_path.unlink, True)
        except Exception:
            logger.exception("OpenAI audio transcription failed (id=%s)", document.id)
            raise

        logger.info("OpenAI audio transcribed (id=%s) in %.2fs", document.id, time.time() - start)

        text = text.strip()
        text_blocks = [TextBlock(text=text, page_number=1)] if text else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            metadata=dict(document.metadata),
            page_count=1 if text else 0,
        )

    async def _prepare_upload(self, input_path: Path) -> tuple[Path, bool]:
        """Return ``(path_to_upload, needs_cleanup)``.

        Files in :attr:`_direct_upload_suffixes` are sent as-is; others
        are decoded by ``pydub`` (ffmpeg) and re-exported as WAV next to
        the input — the caller unlinks that temporary on the way out.
        """
        if input_path.suffix.lower() in self._direct_upload_suffixes:
            return input_path, False

        sound = await asyncio.to_thread(AudioSegment.from_file, input_path)
        logger.info("Converting audio to WAV (duration=%.1fs)", len(sound) / 1000)
        wav_path = input_path.with_suffix(".wav")
        await asyncio.to_thread(sound.export, wav_path, format="wav")
        return wav_path, True

    async def _resolve_prompt(self) -> str | None:
        """Resolve the current managed transcription prompt, if one is wired.

        Prompt resolution happens for every file instead of at client creation
        time, so an Admin UI edit is visible to the next audio request without
        a worker restart. A lookup failure degrades to the endpoint's native
        transcription behaviour rather than failing indexing.
        """
        if self._transcription_prompt_resolver is None:
            return None
        try:
            prompt = await self._transcription_prompt_resolver()
        except Exception as exc:  # noqa: BLE001 - prompt storage must not block transcription
            logger.warning("Transcription prompt resolution failed: %s", exc)
            return None
        return prompt.strip() if prompt and prompt.strip() else None

    def _resolve_transcription_endpoint(self) -> ModelEndpointConfig | None:
        """Resolve the current STT default, falling back safely to env config.

        The resolver points at the mutable in-memory endpoint registry. That
        lets API-process writes take effect immediately and indexer workers use
        their normal registry refresh cycle, without putting database access in
        the per-request audio client.
        """
        if self._transcription_endpoint_resolver is None:
            return None
        try:
            endpoint = self._transcription_endpoint_resolver()
        except Exception as exc:  # noqa: BLE001 - endpoint lookup must not block indexing
            logger.warning("STT endpoint resolution failed: %s", exc)
            return None
        if endpoint is None:
            return None
        if not endpoint.endpoint or not endpoint.model_name:
            logger.warning("Ignoring incomplete configured STT endpoint; using TRANSCRIBER_* fallback.")
            return None
        return endpoint

    def _semaphore_for_endpoint(self, endpoint: ModelEndpointConfig | None) -> asyncio.Semaphore:
        """Return the concurrency limiter selected by the current STT endpoint."""
        if endpoint is None:
            return self._semaphore
        limit = max(1, endpoint.batch_size)
        key = (endpoint.endpoint, endpoint.model_name or "", limit)
        semaphore = self._endpoint_semaphores.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            self._endpoint_semaphores[key] = semaphore
        return semaphore

    @staticmethod
    def _language_hint(endpoint: ModelEndpointConfig | None) -> str | None:
        """Read the optional STT language hint from the registered endpoint."""
        if endpoint is None:
            return None
        language = endpoint.extra.get(STT_LANGUAGE_KEY)
        if isinstance(language, str) and language.strip():
            return language.strip()
        return None

    def _client_for_endpoint(self, endpoint: ModelEndpointConfig | None) -> tuple[AsyncOpenAI, str]:
        """Return an OpenAI client and model for one transcription request."""
        if endpoint is None:
            return self._client, self._model

        api_key = endpoint.extra.get("api_key")
        resolved_api_key = api_key if isinstance(api_key, str) else ""
        if (
            endpoint.endpoint == self._base_url
            and resolved_api_key == self._api_key
            and endpoint.timeout == self._timeout
        ):
            return self._client, endpoint.model_name or self._model

        key = (endpoint.endpoint, resolved_api_key, endpoint.timeout)
        client = self._endpoint_clients.get(key)
        if client is None:
            client = AsyncOpenAI(
                base_url=endpoint.endpoint,
                api_key=resolved_api_key,
                timeout=endpoint.timeout,
            )
            self._endpoint_clients[key] = client
        return client, endpoint.model_name or self._model

    async def _transcribe(
        self,
        path: Path,
        *,
        language: str | None,
        prompt: str | None,
        endpoint_config: ModelEndpointConfig | None = None,
    ) -> str:
        client, model = self._client_for_endpoint(endpoint_config)
        kwargs: dict[str, object] = {"model": model, "file": path}
        if prompt:
            kwargs["prompt"] = prompt
        if language:
            kwargs["language"] = language
        response = await client.audio.transcriptions.create(**kwargs)
        return response.text or ""
