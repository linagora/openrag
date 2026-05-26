"""
Local Whisper-backed audio loader.

The Ray actor + pool that drive ``faster-whisper`` (``WhisperActor``,
``WhisperPool``) and the services-side :class:`BasePooledParser`
implementation now live in
``services/workers/parsers/whisper_workers.py``; this module re-exports
``WhisperActor`` and ``WhisperPool`` for legacy import paths
(``components.indexer.loaders.audio.local_whisper.WhisperActor`` is
still used by the OpenAI audio loader for language detection, and by
``services/workers/bootstrap.py`` for the actor bootstrap).

``LocalWhisperLoader`` is now a thin :class:`BaseLoader` adapter that
delegates to
:class:`core.indexing.parsers.audio.local_whisper.LocalWhisperParser`,
which itself wraps the services-side pool. New code should call the
core parser directly; this shim keeps the legacy loader-discovery path
alive until consumers migrate.
"""

import asyncio
from pathlib import Path

from core.indexing.parsers.audio.local_whisper import LocalWhisperParser
from core.models.document import Document as CoreDocument
from langchain_core.documents.base import Document
from services.workers.parsers.whisper_workers import (  # noqa: F401  (re-exported for legacy import paths)
    LocalWhisperLoader as _ServicesWhisperPool,
)
from services.workers.parsers.whisper_workers import (  # noqa: F401
    WhisperActor,
    WhisperPool,
)
from utils.logger import get_logger

from ..base import BaseLoader

logger = get_logger()


class LocalWhisperLoader(BaseLoader):
    """Adapter shim — delegates to ``LocalWhisperParser`` via the services-side pool."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._parser = LocalWhisperParser(pool=_ServicesWhisperPool())

    async def aload_document(self, file_path, metadata: dict = None, save_markdown=False):
        path = Path(file_path)
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=CoreDocument.detect_content_type(path.name),
            raw_bytes=raw_bytes,
            metadata=dict(metadata) if metadata else {},
        )
        try:
            processed = await self._parser.parse(core_doc)
        except Exception as e:
            logger.error("Error loading document", error=str(e))
            raise

        content = "".join(b.text for b in processed.text_blocks)
        doc = Document(page_content=content, metadata=dict(metadata) if metadata else {})
        if save_markdown:
            self.save_content(content, str(file_path))
        return doc
