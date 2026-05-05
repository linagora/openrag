"""HTML ``DocumentParser`` implementation.

Converts HTML to Markdown via the project's ``html_to_markdown`` dep
(already used by the websearch and pptx pipelines), then emits a single
text block. No file I/O, no JavaScript execution, no image fetching —
purely structural conversion.
"""

from __future__ import annotations

import asyncio

from ...models.document import Document, DocumentType, ProcessedDocument, TextBlock
from ..text_preprocessor import decode_bytes
from .document_parser import DocumentParser


class HtmlParser(DocumentParser):
    """Parse HTML documents into a single Markdown text block."""

    def __init__(self, *, encoding: str | None = None) -> None:
        """``encoding`` forces a specific decode of ``raw_bytes``; ``None``
        auto-detects (UTF-8 first, then chardet).
        """
        self._encoding = encoding

    def supported_types(self) -> list[str]:
        return [DocumentType.HTML.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        markdown = (await asyncio.to_thread(self._html_to_markdown, document)).strip()
        text_blocks = [TextBlock(text=markdown, page_number=1)] if markdown else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            metadata=dict(document.metadata),
            page_count=1 if markdown else 0,
        )

    def _html_to_markdown(self, document: Document) -> str:
        """Decode + HTML→Markdown in one shot. Sync; runs in a thread."""
        html = self._extract_html(document)
        return self._to_markdown(html) if html else ""

    def _extract_html(self, document: Document) -> str:
        if document.text is not None:
            return document.text
        if document.raw_bytes:
            return decode_bytes(document.raw_bytes, encoding=self._encoding)
        return ""

    @staticmethod
    def _to_markdown(html: str) -> str:
        from html_to_markdown import convert

        return convert(html)
