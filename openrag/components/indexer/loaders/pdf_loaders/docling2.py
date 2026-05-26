"""Docling-backed PDF loader.

The Ray actor + pool (``DoclingWorker``, ``DoclingPool``) and the
services-side :class:`BasePooledParser` implementation now live in
``services/workers/parsers/docling_workers.py``; this module re-exports
them for legacy import paths (``services.workers.bootstrap`` constructs the
named ``DoclingPool`` actor at startup via ``get_or_create_actor``).

``DoclingLoader2`` is a thin :class:`BaseLoader` adapter that delegates
to :class:`core.indexing.parsers.pdf.docling.DoclingParser`, which
wraps the services-side pool.  New code should call the core parser
directly; this shim keeps the legacy loader-discovery path alive until
consumers migrate.
"""

from __future__ import annotations

from langchain_core.documents.base import Document
from services.workers.parsers.docling_workers import (  # noqa: F401  (re-exported for legacy paths)
    DoclingLoader,
    DoclingPool,
    DoclingWorker,
)
from utils.logger import get_logger

from ..base import BaseLoader

logger = get_logger()


class DoclingLoader2(BaseLoader):
    """Adapter shim — delegates to ``DoclingParser`` via the services-side pool."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        from core.indexing.parsers.pdf.docling import DoclingParser
        from services.workers.parsers.docling_workers import DoclingLoader as _DoclingLoader

        self._parser = DoclingParser(pool=_DoclingLoader())

    async def aload_document(self, file_path, metadata, save_markdown=False):
        import asyncio
        from pathlib import Path

        from core.models.document import Document as CoreDocument
        from core.models.document import DocumentType

        path = Path(file_path)
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.PDF,
            raw_bytes=raw_bytes,
            metadata=dict(metadata or {}),
        )
        processed = await self._parser.parse(core_doc)

        markdown = ""
        for block in processed.text_blocks:
            markdown += block.text + f"\n[PAGE_{block.page_number}]\n"

        if self.image_captioning and processed.images:
            import io

            from PIL import Image

            pil_images = []
            for img_block in processed.images:
                if img_block.image_bytes:
                    pil_images.append(Image.open(io.BytesIO(img_block.image_bytes)))

            if pil_images:
                captions = await self.caption_images(pil_images)
                for img_block, caption in zip(processed.images, captions):
                    ref = (img_block.metadata or {}).get("markdown_ref")
                    if ref:
                        markdown = markdown.replace("<!-- image -->", caption, 1)
        else:
            logger.debug("Image captioning disabled. Ignoring images.")

        doc = Document(page_content=markdown, metadata=metadata)
        if save_markdown:
            self.save_document(Document(page_content=markdown), str(file_path))
        return doc
