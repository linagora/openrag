"""
Marker-backed PDF loader.

The Ray actor + pool that drive Marker (``MarkerWorker``,
``MarkerPool``) and the services-side :class:`BasePooledParser`
implementation now live in
``services/workers/parsers/marker_workers.py``; this module re-exports
``MarkerWorker`` and ``MarkerPool`` for legacy import paths
(``services.workers.bootstrap`` constructs the named ``MarkerPool`` actor at
startup via ``get_or_create_actor``).

``MarkerLoader`` is now a thin :class:`BaseLoader` adapter that
delegates to :class:`core.indexing.parsers.pdf.marker.MarkerParser`,
which itself wraps the services-side pool. New code should call the
core parser directly; this shim keeps the legacy loader-discovery path
alive until consumers migrate.
"""

import asyncio
import time
from io import BytesIO
from pathlib import Path

from core.indexing.parsers.pdf.marker import MarkerParser
from core.models.document import Document as CoreDocument
from core.models.document import DocumentType
from langchain_core.documents.base import Document
from PIL import Image
from services.workers.parsers.marker_workers import (  # noqa: F401  (re-exported for legacy import paths)
    MarkerLoader as _ServicesMarkerPool,
)
from services.workers.parsers.marker_workers import (  # noqa: F401
    MarkerPool,
    MarkerWorker,
)
from utils.logger import get_logger

from ..base import BaseLoader

logger = get_logger()


class MarkerLoader(BaseLoader):
    """Adapter shim — delegates to ``MarkerParser`` via the services-side pool."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.page_sep = "[PAGE_SEP]"
        self._parser = MarkerParser(pool=_ServicesMarkerPool())

    async def aload_document(
        self,
        file_path: str | Path,
        metadata: dict | None = None,
        save_markdown: bool = False,
    ) -> Document:
        if metadata is None:
            metadata = {}

        path = Path(file_path)
        file_path_str = str(file_path)
        start = time.time()

        try:
            raw_bytes = await asyncio.to_thread(path.read_bytes)
            core_doc = CoreDocument(
                filename=path.name,
                content_type=DocumentType.PDF,
                raw_bytes=raw_bytes,
                metadata=dict(metadata),
            )
            processed = await self._parser.parse(core_doc)

            markdown = "".join(f"{b.text}\n[PAGE_{b.page_number}]\n" for b in processed.text_blocks)
            if not markdown:
                raise RuntimeError(f"Conversion failed for {file_path_str}")

            if not self.image_captioning:
                logger.debug("Image captioning disabled.")
                for block in processed.images:
                    ref = (block.metadata or {}).get("markdown_ref")
                    if ref:
                        markdown = markdown.replace(ref, "")
            elif processed.images:
                pil_images: list[Image.Image] = []
                for block in processed.images:
                    img = Image.open(BytesIO(block.image_bytes))
                    img.load()
                    pil_images.append(img)
                captions = await self.caption_images(pil_images)
                for block, caption in zip(processed.images, captions):
                    ref = (block.metadata or {}).get("markdown_ref")
                    if ref:
                        markdown = markdown.replace(ref, caption)

            doc = Document(page_content=markdown, metadata=metadata)
            if save_markdown:
                self.save_content(markdown, file_path_str)

            duration = time.time() - start
            logger.info(f"Processed {file_path_str} in {duration:.2f}s")
            return doc

        except Exception:
            logger.exception("Error in aload_document", path=file_path_str)
            raise
