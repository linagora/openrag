"""PyMuPDF-backed PDF ``DocumentParser``.

The lightweight, no-VLM, no-GPU PDF backend. Uses ``pymupdf`` (a.k.a.
``fitz``) for plain-text extraction and ``pymupdf4llm`` for Markdown
extraction. Operates on ``Document.raw_bytes`` — file I/O is upstream.

In ``mode="markdown"``, embedded images are surfaced as ``ImageBlock``s
via ``pymupdf4llm``'s ``embed_images=True`` (each image becomes a
``data:image/png;base64,…`` ref in the markdown, which we decode into
an :class:`ImageBlock` with ``markdown_ref`` set so a downstream caption
stage can substitute a description back in). ``mode="text"`` does not
extract images.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import pymupdf
import pymupdf4llm

from ....models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from ...image_preprocessor import extract_data_uri_image_blocks
from ..document_parser import DocumentParser

ParseMode = Literal["markdown", "text"]


def _extract_text(raw: bytes) -> tuple[list[str], list[ImageBlock]]:
    """Return one stripped plain-text string per page; no images."""
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        return [page.get_text().strip() for page in doc], []


def _extract_markdown(raw: bytes) -> tuple[list[str], list[ImageBlock]]:
    """Return Markdown per page + ``ImageBlock``s built from embedded data URIs.

    ``embed_images=True`` makes ``pymupdf4llm`` write images as base64
    data URIs in-line. We decode each ref into an ``ImageBlock`` and
    leave the ref in the page text untouched so the caption stage can
    substitute later via ``ImageBlock.metadata['markdown_ref']``.
    """
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        chunks = pymupdf4llm.to_markdown(
            doc,
            page_chunks=True,
            embed_images=True,
            write_images=False,
            dpi=300,
        )
    pages: list[str] = []
    images: list[ImageBlock] = []
    for i, chunk in enumerate(chunks, start=1):
        text = (chunk.get("text") or "").strip()
        pages.append(text)
        if text:
            images.extend(extract_data_uri_image_blocks(text, page_number=i))
    return pages, images


class PyMuPDFParser(DocumentParser):
    """Extract text from a PDF as one ``TextBlock`` per page (+ ImageBlocks in markdown mode).

    ``mode="markdown"`` (default) uses ``pymupdf4llm`` for layout-preserving
    Markdown — better for downstream embedding and chunking, and surfaces
    embedded images. ``mode="text"`` uses raw ``pymupdf`` for plain text —
    slightly faster, no formatting, no images.
    """

    def __init__(self, *, mode: ParseMode = "markdown") -> None:
        if mode not in ("markdown", "text"):
            raise ValueError(f"PyMuPDFParser: unsupported mode {mode!r}")
        self._mode = mode
        self._extract = _extract_text if mode == "text" else _extract_markdown

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        pages, images = await asyncio.to_thread(self._extract, document.raw_bytes)
        text_blocks = [TextBlock(text=text, page_number=i) for i, text in enumerate(pages, start=1) if text]
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=dict(document.metadata),
            page_count=len(pages),
        )
