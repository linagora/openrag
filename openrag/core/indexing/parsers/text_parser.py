"""Plain-text ``DocumentParser`` implementation.

Decodes ``Document.raw_bytes`` (or uses ``Document.text`` if already
populated) into a single :class:`TextBlock`. Performs no image captioning,
no markdown-image extraction, and no file I/O — those are upstream
concerns. Handles the ``TEXT`` content type; Markdown (with image
captioning) lives in :class:`MarkdownParser`; HTML, PDF, and richer
formats live in their own parsers.
"""

from __future__ import annotations

import asyncio

from ...models.document import Document, DocumentType, ProcessedDocument, TextBlock
from ..text_preprocessor import decode_bytes
from .document_parser import DocumentParser


class TextParser(DocumentParser):
    """Parse plain-text documents into a single text block."""

    def __init__(self, *, encoding: str | None = None) -> None:
        """If ``encoding`` is ``None``, raw bytes are auto-detected via
        :func:`core.indexing.text_preprocessor.decode_bytes`.
        """
        self._encoding = encoding

    def supported_types(self) -> list[str]:
        return [DocumentType.TEXT.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        text = (await asyncio.to_thread(self._extract_text, document)).strip()
        text_blocks = [TextBlock(text=text, page_number=1)] if text else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            metadata=dict(document.metadata),
            page_count=1 if text else 0,
        )

    def _extract_text(self, document: Document) -> str:
        if document.text is not None:
            return document.text
        if document.raw_bytes:
            return decode_bytes(document.raw_bytes, encoding=self._encoding)
        return ""
