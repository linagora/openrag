"""
PPTX file loader implementation.

``PPTXLoader`` is now a thin :class:`BaseLoader` adapter that delegates
extraction to :class:`core.indexing.parsers.pptx_parser.PptxParser` and
layers VLM captioning of slide pictures on top via the ``BaseLoader``
mixin. New code should call the core parser directly; this shim keeps
the legacy loader-discovery path alive until consumers migrate.
"""

import asyncio
from io import BytesIO
from pathlib import Path

from core.indexing.parsers.pptx_parser import PptxParser
from core.models.document import Document as CoreDocument
from core.models.document import DocumentType
from langchain_core.documents.base import Document
from PIL import Image
from utils.logger import get_logger

from .base import BaseLoader

logger = get_logger()


class PPTXLoader(BaseLoader):
    """Adapter shim — delegates to ``PptxParser``; layers image captioning on top."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._parser = PptxParser()

    async def aload_document(self, file_path, metadata=None, save_markdown=False):
        metadata = {} if metadata is None else dict(metadata)
        path = Path(file_path)
        raw_bytes = await asyncio.to_thread(path.read_bytes)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.PPTX,
            raw_bytes=raw_bytes,
            metadata=dict(metadata) if metadata else {},
        )
        processed = await self._parser.parse(core_doc)

        # Reconstitute the legacy ``<slide>\n[PAGE_N]\n`` page-anchored layout.
        slides = [f"{b.text}\n[PAGE_{b.page_number}]" for b in processed.text_blocks]
        md_content = ("\n".join(slides) + "\n") if slides else ""

        if not self.image_captioning:
            logger.info("Image captioning disabled. Ignoring images.")
            for block in processed.images:
                ref = (block.metadata or {}).get("markdown_ref")
                if ref:
                    md_content = md_content.replace(ref, "")
        elif processed.images:
            pil_images: list[Image.Image] = []
            for block in processed.images:
                img = Image.open(BytesIO(block.image_bytes))
                img.load()
                pil_images.append(img)
            captions = await self.caption_images(pil_images, desc="Generating captions")
            for block, caption in zip(processed.images, captions):
                ref = (block.metadata or {}).get("markdown_ref")
                if ref:
                    md_content = md_content.replace(ref, caption.replace("\\", "/"))

        doc = Document(page_content=md_content, metadata=metadata)
        if save_markdown:
            self.save_content(md_content, str(file_path))
        return doc
