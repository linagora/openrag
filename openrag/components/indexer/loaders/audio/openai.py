"""
OpenAI-compatible audio loader.

The transcription client now lives in
``services/inference/parsers/openai_audio.py`` as
:class:`OpenAIAudioClient` (a :class:`BaseClientParser`).
``OpenAIAudioLoader`` is a thin :class:`BaseLoader` adapter that
constructs the services-side client (with a Whisper-actor-backed
language detector when ``transcriber.use_whisper_lang_detector`` is
enabled) and wraps it in
:class:`core.indexing.parsers.audio.client_based.ClientAudioParser`.
New code should call the core parser directly; this shim keeps the
legacy loader-discovery path alive until consumers migrate.
"""

import asyncio
from pathlib import Path

from core.indexing.parsers.audio.client_based import ClientAudioParser
from core.models.document import Document as CoreDocument
from core.models.document import DocumentType
from langchain_core.documents.base import Document
from pydub import AudioSegment
from services.inference.parsers.openai_audio import OpenAIAudioClient
from utils.logger import get_logger

from ..base import BaseLoader
from .local_whisper import WhisperActor

logger = get_logger()

# Duration of the audio sample used for language detection
LANG_DETECT_SAMPLE_MS = 30_000  # 30 s


def _get_whisper_actor():
    try:
        return WhisperActor.options(name="WhisperActor", namespace="openrag", get_if_exists=True).remote()
    except Exception as e:
        logger.error("Error getting WhisperActor", error=str(e))
        raise


async def _whisper_language_detector(file_path: Path) -> str | None:
    """Detect language via the singleton ``WhisperActor`` from a short audio sample."""
    sound = await asyncio.to_thread(AudioSegment.from_file, file_path)
    sample = sound[:LANG_DETECT_SAMPLE_MS]
    tmp_path = file_path.parent / f"{file_path.stem}_langdetect.wav"
    await asyncio.to_thread(sample.export, tmp_path, format="wav")
    try:
        whisper_actor = _get_whisper_actor()
        return await whisper_actor.detect_language.remote(tmp_path, "en")
    except Exception as e:
        logger.exception("Language detection failed", error=str(e))
        return None
    finally:
        await asyncio.to_thread(tmp_path.unlink, True)


class OpenAIAudioLoader(BaseLoader):
    """Adapter shim — delegates to ``OpenAIAudioClient`` via ``ClientAudioParser``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        cfg = self.config.loader.transcriber
        _client = OpenAIAudioClient(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model_name,
            timeout=cfg.timeout,
            direct_upload_suffixes=cfg.direct_upload_suffixes,
            language_detector=_whisper_language_detector if cfg.use_whisper_lang_detector else None,
        )
        self._parser = ClientAudioParser(client=_client)

    async def aload_document(self, file_path, metadata: dict = None, save_markdown=False):
        if metadata is None:
            metadata = {}
        path = Path(file_path)
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.AUDIO,
            raw_bytes=raw_bytes,
            metadata=dict(metadata),
        )
        try:
            processed = await self._parser.parse(core_doc)
        except Exception:
            logger.exception("Error in OpenAIAudioLoader", path=str(file_path))
            raise
        content = "".join(b.text for b in processed.text_blocks)
        doc = Document(page_content=content, metadata=metadata)
        if save_markdown:
            self.save_content(content, str(file_path))
        return doc
