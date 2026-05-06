"""OpenAI-compatible audio transcription client.

Pipeline:

1. Materialize ``Document.raw_bytes`` to a temporary file via
   :meth:`Document.as_temporary_file`.
2. If the file's suffix is in ``direct_upload_suffixes``, send it to the
   transcription endpoint as-is. Otherwise, decode through
   ``pydub.AudioSegment`` and re-encode as WAV (libsndfile-friendly).
3. Optionally run a caller-provided language detector against the
   prepared file (its result is forwarded to the OpenAI ``language``
   param). The detector is a plain async callable so this client stays
   free of Ray / model-loader coupling — the wiring layer can plug in a
   Whisper actor or any other implementation.
4. Send the file to ``audio.transcriptions.create`` and emit a single
   :class:`TextBlock` with the resulting transcript.

Adapted from the legacy
``components/indexer/loaders/audio/openai.py`` ``AudioTranscriber``;
the new version drops the in-memory ``components.utils`` semaphore (now
per-instance via ``concurrency_limit``) and the embedded WhisperActor
ref-getter (now an injected callable).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from core.indexing.parsers.document_parser import BaseClientParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from openai import AsyncOpenAI
from pydub import AudioSegment

logger = logging.getLogger(__name__)


# Suffixes the transcription backend can ingest as-is, avoiding the ~10x
# size inflation from WAV conversion (Scaleway cap: 100 MB; OpenAI: 25 MB).
_DEFAULT_DIRECT_UPLOAD_SUFFIXES: tuple[str, ...] = (".mp3", ".m4a", ".ogg", ".webm", ".wav")

LanguageDetector = Callable[[Path], Awaitable[str | None]]


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
        concurrency_limit: int = 1,
    ) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model
        self._direct_upload_suffixes = {s.lower() for s in direct_upload_suffixes}
        self._language_detector = language_detector
        self._semaphore = asyncio.Semaphore(max(1, concurrency_limit))

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
                async with self._semaphore:
                    upload_path, cleanup = await self._prepare_upload(input_path)
                    try:
                        language: str | None = None
                        if self._language_detector is not None:
                            try:
                                language = await self._language_detector(upload_path)
                            except Exception as exc:
                                logger.warning("Language detection failed: %s", exc)
                        text = await self._transcribe(upload_path, language=language)
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

    async def _transcribe(self, path: Path, *, language: str | None) -> str:
        kwargs: dict[str, object] = {"model": self._model, "file": path}
        if language:
            kwargs["language"] = language
        response = await self._client.audio.transcriptions.create(**kwargs)
        return response.text or ""
