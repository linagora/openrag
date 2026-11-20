"""HTML ``DocumentParser`` implementation.

Converts HTML to Markdown via the project's ``html_to_markdown`` dep
(already used by the websearch and pptx pipelines), then emits a text
block plus image blocks for embedded data-URI images. The base64 payloads
are replaced with compact caption-compatible references before chunking.
No file I/O, JavaScript execution, or image fetching is performed.
"""

from __future__ import annotations

import asyncio
import re

from bs4 import BeautifulSoup
from bs4.dammit import EntitySubstitution
from bs4.formatter import HTMLFormatter
from core.indexing.image_preprocessor import normalize_data_uri_images
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock

from ..text_preprocessor import decode_bytes
from .document_parser import DocumentParser
from .registry import parser_registry


class _LiteralAttributeValue(str):
    """Mark an HTML attribute value that must not undergo entity substitution."""


class _CaptionHTMLFormatter(HTMLFormatter):
    """Preserve literal image attributes while escaping HTML text nodes."""

    def attributes(self, tag):
        for name, value in super().attributes(tag):
            if tag.name == "img" and name in {"alt", "src"} and value is not None:
                value = _LiteralAttributeValue(value)
            yield name, value

    def substitute(self, value):
        if isinstance(value, _LiteralAttributeValue):
            return str(value)
        return super().substitute(value)


@parser_registry.register("html")
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
        markdown, images = normalize_data_uri_images(markdown, page_number=1, reference_scope=document.id)
        text_blocks = [TextBlock(text=markdown, page_number=1)] if markdown else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
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

        if re.search(r"<caption(?=[\s/>])", html, re.IGNORECASE):
            soup = BeautifulSoup(html, "html5lib")

            formatter = _CaptionHTMLFormatter(
                entity_substitution=EntitySubstitution.substitute_html,
                void_element_close_prefix=None,
            )

            html = soup.decode(formatter=formatter)
        return convert(html)
