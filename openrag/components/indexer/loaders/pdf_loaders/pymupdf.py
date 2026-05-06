"""
PyMuPDF-backed PDF loader implementation.

``PyMuPDFLoader`` and ``PyMuPDF4LLMLoader`` are now thin
:class:`BaseLoader` adapters that delegate to
:class:`core.indexing.parsers.pdf.pymupdf.PyMuPDFParser` (text and
markdown modes respectively). The markdown adapter additionally layers
VLM captioning of embedded images on top via the ``BaseLoader`` mixin.
New code should call the core parser directly; this shim keeps the
legacy loader-discovery path alive until consumers migrate.
"""

import asyncio
from io import BytesIO
from pathlib import Path

from core.indexing.parsers.pdf.pymupdf import PyMuPDFParser
from core.models.document import Document as CoreDocument
from core.models.document import DocumentType
from langchain_core.documents.base import Document
from PIL import Image
from utils.logger import get_logger

from ..base import BaseLoader

logger = get_logger()


def _join_pages_with_anchors(text_blocks) -> str:
    """Join one ``TextBlock`` per page with the legacy ``\\n[PAGE_N]\\n`` anchors."""
    return "".join(f"{b.text}\n[PAGE_{b.page_number}]\n" for b in text_blocks)


async def _read_pdf_bytes(file_path) -> tuple[Path, bytes]:
    path = Path(file_path)
    raw_bytes = await asyncio.to_thread(path.read_bytes)
    return path, raw_bytes


class PyMuPDFLoader(BaseLoader):
    """Adapter shim — delegates to ``PyMuPDFParser(mode='text')``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._parser = PyMuPDFParser(mode="text")

    async def aload_document(self, file_path, metadata: dict = None, save_markdown=False):
        metadata = {} if metadata is None else dict(metadata)
        path, raw_bytes = await _read_pdf_bytes(file_path)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.PDF,
            raw_bytes=raw_bytes,
            metadata=metadata,
        )
        processed = await self._parser.parse(core_doc)
        s = _join_pages_with_anchors(processed.text_blocks)

        doc = Document(page_content=s, metadata=metadata)
        if save_markdown:
            self.save_content(s, str(file_path))
        return doc


class PyMuPDF4LLMLoader(BaseLoader):
    """Adapter shim — delegates to ``PyMuPDFParser(mode='markdown')``; layers image captioning on top."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._parser = PyMuPDFParser(mode="markdown")

    async def aload_document(self, file_path, metadata: dict = None, save_markdown=False):
        metadata = {} if metadata is None else dict(metadata)
        path, raw_bytes = await _read_pdf_bytes(file_path)
        core_doc = CoreDocument(
            filename=path.name,
            content_type=DocumentType.PDF,
            raw_bytes=raw_bytes,
            metadata=metadata,
        )
        processed = await self._parser.parse(core_doc)
        s = _join_pages_with_anchors(processed.text_blocks)

        if not self.image_captioning:
            # Legacy parity: the old loader called ``pymupdf4llm`` with the
            # default ``embed_images=False`` and surfaced no images. The new
            # parser embeds them as data URIs; strip those refs to match.
            for block in processed.images:
                ref = (block.metadata or {}).get("markdown_ref")
                if ref:
                    s = s.replace(ref, "")
        elif processed.images:
            pil_images: list[Image.Image] = []
            for block in processed.images:
                img = Image.open(BytesIO(block.image_bytes))
                img.load()
                pil_images.append(img)
            captions = await self.caption_images(pil_images, desc="Captioning embedded images")
            for block, caption in zip(processed.images, captions):
                ref = (block.metadata or {}).get("markdown_ref")
                if ref:
                    s = s.replace(ref, caption.replace("\\", "/"))

        doc = Document(page_content=s, metadata=metadata)
        if save_markdown:
            self.save_content(s, str(file_path))
        return doc
