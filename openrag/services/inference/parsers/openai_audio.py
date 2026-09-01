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
import inspect
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from core.config.model_endpoints import CONTROL_EXTRA_KEYS, STT_LANGUAGE_KEY, ModelEndpointConfig
from core.indexing.parsers.document_parser import BaseClientParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from core.utils.logging import get_logger
from openai import AsyncOpenAI
from pydub import AudioSegment

logger = get_logger()


# Suffixes the transcription backend can ingest as-is, avoiding the ~10x
# size inflation from WAV conversion (Scaleway cap: 100 MB; OpenAI: 25 MB).
_DEFAULT_DIRECT_UPLOAD_SUFFIXES: tuple[str, ...] = (".mp3", ".m4a", ".ogg", ".webm", ".wav")
# ``AsyncOpenAI`` requires a non-empty API key even when the upstream endpoint
# is intentionally unauthenticated. This value is the application's established
# non-secret placeholder (see ``TranscriberConfig``).
_ANONYMOUS_API_KEY = "EMPTY"

LanguageDetector = Callable[[Path], Awaitable[str | None]]
TranscriptionPromptResolver = Callable[[], Awaitable[str | None]]
TranscriptionEndpointResolver = Callable[[], ModelEndpointConfig | None | Awaitable[ModelEndpointConfig | None]]

# Endpoint ``extra`` holds both connection metadata and provider request options.
# Only the latter belongs in the OpenAI-compatible transcription request body.
_STT_REQUEST_CONTROL_EXTRA_KEYS = CONTROL_EXTRA_KEYS | frozenset(
    {
        "api_key",
        STT_LANGUAGE_KEY,
        # These are owned by OpenRAG's configured endpoint / prompt plumbing.
        # Streaming is deliberately unsupported because this parser expects one
        # complete transcription response.
        "file",
        "model",
        "prompt",
        "stream",
    }
)


class _EndpointConcurrencyLimiter:
    """An async limiter whose capacity can change without replacing active work."""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self) -> int:
        """Return the current maximum number of concurrent requests."""
        return self._limit

    async def set_limit(self, limit: int) -> None:
        """Apply a new limit before allowing any further queued request."""
        async with self._condition:
            self._limit = max(1, limit)
            self._condition.notify_all()

    async def __aenter__(self) -> _EndpointConcurrencyLimiter:
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()


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
        # Keep only the current dynamically resolved endpoint limiter. Existing
        # requests retain a local reference while they finish, but retired
        # endpoints do not accumulate in long-lived worker processes.
        self._endpoint_limiter: _EndpointConcurrencyLimiter | None = None
        self._endpoint_limiter_key: tuple[str, str] | None = None

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
                endpoint_config = await self._resolve_transcription_endpoint()
                limiter = await self._semaphore_for_endpoint(endpoint_config)
                async with limiter:
                    upload_path, cleanup = await self._prepare_upload(input_path)
                    try:
                        language = self._language_hint(endpoint_config)
                        if language is None and self._language_detector is not None:
                            try:
                                language = await self._language_detector(upload_path)
                            except Exception as exc:
                                logger.bind(error=str(exc)).warning("Language detection failed")
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
            logger.bind(document_id=document.id).exception("OpenAI audio transcription failed")
            raise

        logger.bind(document_id=document.id, elapsed_seconds=round(time.time() - start, 2)).info(
            "OpenAI audio transcribed"
        )

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
        logger.bind(duration_seconds=round(len(sound) / 1000, 1)).info("Converting audio to WAV")
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
            logger.bind(error=str(exc)).warning("Transcription prompt resolution failed")
            return None
        return prompt.strip() if prompt and prompt.strip() else None

    async def _resolve_transcription_endpoint(self) -> ModelEndpointConfig | None:
        """Resolve the current STT default, falling back safely to env config.

        Indexer workers resolve from their refreshed local registry, while the
        direct extraction path may await a fresh registry reload. Keeping that
        distinction behind the resolver lets both paths share this client.
        """
        if self._transcription_endpoint_resolver is None:
            return None
        try:
            endpoint = self._transcription_endpoint_resolver()
            if inspect.isawaitable(endpoint):
                endpoint = await endpoint
        except Exception as exc:  # noqa: BLE001 - endpoint lookup must not block indexing
            logger.bind(error=str(exc)).warning("STT endpoint resolution failed")
            return None
        if endpoint is None:
            return None
        if not endpoint.endpoint or not endpoint.model_name:
            logger.warning("Ignoring incomplete configured STT endpoint; using TRANSCRIBER_* fallback")
            return None
        return endpoint

    async def _semaphore_for_endpoint(
        self, endpoint: ModelEndpointConfig | None
    ) -> asyncio.Semaphore | _EndpointConcurrencyLimiter:
        """Return the current client's per-worker STT concurrency limiter.

        Each indexing worker owns its own client and therefore its own limiter.
        The endpoint value bounds requests from that worker; it is not a
        cluster-wide quota across OpenRAG replicas. Updating a configured
        limit changes the existing limiter, so lowering it never starts extra
        work through a newly minted semaphore.
        """
        if endpoint is None:
            self._endpoint_limiter = None
            self._endpoint_limiter_key = None
            return self._semaphore
        limit = max(1, endpoint.batch_size)
        key = (endpoint.endpoint, endpoint.model_name or "")
        if self._endpoint_limiter is None or self._endpoint_limiter_key != key:
            self._endpoint_limiter = _EndpointConcurrencyLimiter(limit)
            self._endpoint_limiter_key = key
        else:
            await self._endpoint_limiter.set_limit(limit)
        return self._endpoint_limiter

    @staticmethod
    def _language_hint(endpoint: ModelEndpointConfig | None) -> str | None:
        """Read the optional STT language hint from the registered endpoint."""
        if endpoint is None:
            return None
        language = endpoint.extra.get(STT_LANGUAGE_KEY)
        if isinstance(language, str) and language.strip():
            return language.strip()
        return None

    @staticmethod
    def _request_extra(endpoint: ModelEndpointConfig | None) -> dict[str, object]:
        """Return provider-specific transcription request options from endpoint extra.

        The Admin UI deliberately stores generic, non-secret options in
        ``extra`` so MOSS, Whisper, and other OpenAI-compatible providers can
        receive their own request fields without adding a provider-specific
        OpenRAG configuration surface. Connection metadata is excluded here:
        the API key configures the client and the language hint is sent through
        the standard ``language`` parameter.
        """
        if endpoint is None:
            return {}
        return {key: value for key, value in endpoint.extra.items() if key not in _STT_REQUEST_CONTROL_EXTRA_KEYS}

    @staticmethod
    def _response_text(response: object) -> str:
        """Extract transcript text from JSON and plain-text OpenAI responses."""
        if isinstance(response, str):
            return response
        text = getattr(response, "text", None)
        return text if isinstance(text, str) else ""

    def _is_fallback_endpoint(self, endpoint: ModelEndpointConfig) -> bool:
        """Whether *endpoint* is the legacy ``TRANSCRIBER_*`` destination."""
        return endpoint.endpoint.strip().rstrip("/") == self._base_url.strip().rstrip("/")

    def _client_for_endpoint(self, endpoint: ModelEndpointConfig | None) -> tuple[AsyncOpenAI, str, bool]:
        """Return an OpenAI client, model, and whether the client must be closed."""
        if endpoint is None:
            return self._client, self._model, False

        api_key = endpoint.extra.get("api_key")
        # A resolved registry endpoint owns its credentials. In particular, an
        # administrator clearing its key must not silently restore the legacy
        # TRANSCRIBER_API_KEY merely because the endpoint URL happens to match.
        # The environment fallback remains available only when no endpoint was
        # resolved above.
        resolved_api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else _ANONYMOUS_API_KEY
        if (
            self._is_fallback_endpoint(endpoint)
            and resolved_api_key == self._api_key
            and endpoint.timeout == self._timeout
        ):
            return self._client, endpoint.model_name or self._model, False

        client = AsyncOpenAI(
            base_url=endpoint.endpoint,
            api_key=resolved_api_key,
            timeout=endpoint.timeout,
        )
        return client, endpoint.model_name or self._model, True

    async def _transcribe(
        self,
        path: Path,
        *,
        language: str | None,
        prompt: str | None,
        endpoint_config: ModelEndpointConfig | None = None,
    ) -> str:
        client, model, close_client = self._client_for_endpoint(endpoint_config)
        kwargs: dict[str, object] = {"model": model, "file": path}
        if prompt:
            kwargs["prompt"] = prompt
        if language:
            kwargs["language"] = language
        request_extra = self._request_extra(endpoint_config)
        # The OpenAI SDK returns a plain string for ``response_format=text``.
        # Pass that standard option through its typed argument so both its
        # request encoding and response handling stay compatible with the SDK.
        response_format = request_extra.pop("response_format", None)
        if isinstance(response_format, str) and response_format.strip():
            kwargs["response_format"] = response_format
        elif response_format is not None:
            request_extra["response_format"] = response_format
        if request_extra:
            kwargs["extra_body"] = request_extra
        logger.bind(
            model=model,
            language=language or "auto",
            configured_stt_endpoint=endpoint_config is not None,
        ).info("Sending audio transcription request")
        try:
            response = await client.audio.transcriptions.create(**kwargs)
            transcript = self._response_text(response)
            return transcript
        finally:
            if close_client:
                try:
                    await client.close()
                except Exception as exc:  # noqa: BLE001 - cleanup must not hide a transcription result
                    logger.bind(error=str(exc)).warning("Failed to close temporary STT client")
