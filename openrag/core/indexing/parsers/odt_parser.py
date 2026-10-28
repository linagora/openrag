"""ODT (OpenDocument text) ``DocumentParser`` implementation.

Reads ``content.xml`` directly (see :mod:`.odf`) and emits one Markdown
:class:`TextBlock`, like the DOCX parser: headings, paragraphs, lists and
tables. Embedded pictures become ``![](odt-image-N)`` refs, each with a
matching :class:`ImageBlock`.

Captioning is not done here — see :class:`ImageBlock` for the
parser→caption contract.

A package that cannot be read degrades to an empty document rather than
raising, as the DOCX and PPTX parsers do.
"""

from __future__ import annotations

import asyncio
import logging

from ...models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from .document_parser import DocumentParser
from .odf import OdfConverter, OdfError, open_odf
from .registry import parser_registry

logger = logging.getLogger(__name__)


@parser_registry.register("odt")
class OdtParser(DocumentParser):
    """Parse ODT into a Markdown TextBlock + one ImageBlock per embedded picture."""

    def supported_types(self) -> list[str]:
        return [DocumentType.ODT.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes and not document.source_path:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        async with document.as_temporary_file() as path:
            markdown, images = await asyncio.to_thread(self._convert, str(path))

        markdown = markdown.strip()
        text_blocks = [TextBlock(text=markdown, page_number=1)] if markdown else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=dict(document.metadata),
            page_count=1 if markdown else 0,
        )

    @staticmethod
    def _convert(path: str) -> tuple[str, list[ImageBlock]]:
        try:
            with open_odf(path) as package:
                converter = OdfConverter(package, image_prefix="odt-image")
                return converter.text_markdown(), converter.images
        except OdfError as exc:
            logger.warning("Failed to read ODT: %s", exc)
            return "", []
