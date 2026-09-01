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

Threading note: PyMuPDF is **not** thread-safe — concurrent calls to
``page.get_text`` / ``pymupdf4llm.to_markdown`` from different threads
can raise ``ValueError: not a textpage of this page`` (upstream
maintainer position: documented limitation, won't fix). We therefore
serialize all PyMuPDF parsing and layout-evidence work onto the shared
executor in ``pymupdf_runtime``. The async ``parse`` method stays concurrent:
multiple callers queue on that executor, but only one PyMuPDF operation runs
at a time.
"""

from __future__ import annotations

from typing import Literal

import pymupdf
import pymupdf4llm
from core.utils.logging import get_logger

from ....models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from ..document_parser import DocumentParser
from ..registry import parser_registry
from .pymupdf_runtime import run_pymupdf

ParseMode = Literal["markdown", "text"]

logger = get_logger()


def _extract_text(raw: bytes, filename: str) -> tuple[list[str], list[ImageBlock]]:
    """Return one stripped plain-text string per page; no images."""
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        return [page.get_text().strip() for page in doc], []


def _to_markdown(doc: pymupdf.Document) -> list[dict]:
    return pymupdf4llm.to_markdown(doc, page_chunks=True, embed_images=False, write_images=False)


def _extract_markdown(raw: bytes, filename: str) -> tuple[list[str], list[ImageBlock]]:
    """Return structured Markdown per page (no images).

    pymupdf is the lightweight, no-VLM backend. ``pymupdf4llm`` preserves
    document structure (headings, lists, tables) — which the markdown-aware
    chunker needs to cut on real boundaries instead of mid-sentence — while
    ``embed_images=False`` keeps base64 image data out of the text. That keeps
    chunks small (no Milvus gRPC overflow) and skips image rendering entirely
    (fast). Image-aware parsing is marker/docling's job, so no ``ImageBlock``s
    are produced here.
    """
    with pymupdf.open(stream=raw, filetype="pdf") as doc:
        try:
            chunks = _to_markdown(doc)
        except RuntimeError as exc:
            # MuPDF hard-errors on some legal-but-unusual object graphs — e.g.
            # Type3 fonts with no embedded font file trip "code=4: no font file
            # for digest" on a single page and take the whole document down
            # with them (openrag#640). `garbage=4, clean=True` rewrites the PDF,
            # dropping unreferenced/orphaned objects (including the bad font
            # refs) without touching visible content, and recovers the full
            # document — retry once against that cleaned copy before giving up.
            logger.bind(filename=filename, error=str(exc)).warning(
                "pymupdf4llm.to_markdown failed; retrying against a garbage-collected/cleaned copy"
            )
            cleaned = doc.tobytes(garbage=4, clean=True)
            with pymupdf.open(stream=cleaned, filetype="pdf") as clean_doc:
                chunks = _to_markdown(clean_doc)
    pages = [(chunk.get("text") or "").strip() for chunk in chunks]
    return pages, []


@parser_registry.register("pymupdf")
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

        pages, images = await run_pymupdf(self._extract, document.raw_bytes, document.filename)
        # Keep one TextBlock per source page (including empties) so callers
        # can preserve a 1-to-1 mapping with the original PDF's pagination.
        text_blocks = [TextBlock(text=text, page_number=i) for i, text in enumerate(pages, start=1)]
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=dict(document.metadata),
            page_count=len(pages),
        )
