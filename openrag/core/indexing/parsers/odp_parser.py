"""ODP (OpenDocument presentation) ``DocumentParser`` implementation.

Converts through ``xberg`` (see :mod:`.odf`) and emits one
:class:`TextBlock` per slide (1-indexed ``page_number``), like the PPTX
parser: the title shape → ``#`` heading, then text boxes, tables and
pictures in document order, with speaker notes appended as ``### Notes:``.
Pictures become ``![](odp-image-N)`` refs; each :class:`ImageBlock` carries
the slide number it came from.

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
from .odf import OdfError, read_odf, slides_markdown
from .registry import parser_registry

logger = logging.getLogger(__name__)


@parser_registry.register("odp")
class OdpParser(DocumentParser):
    """Parse ODP into one TextBlock per slide plus one ImageBlock per picture."""

    def supported_types(self) -> list[str]:
        return [DocumentType.ODP.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes and not document.source_path:
            return ProcessedDocument(
                document_id=document.id,
                metadata=dict(document.metadata),
            )

        async with document.as_temporary_file() as path:
            slide_count, slides, images = await asyncio.to_thread(self._convert, str(path))

        text_blocks = [TextBlock(text=text, page_number=slide_num) for slide_num, text in slides]
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=dict(document.metadata),
            page_count=slide_count,
        )

    @staticmethod
    def _convert(path: str) -> tuple[int, list[tuple[int, str]], list[ImageBlock]]:
        try:
            return slides_markdown(read_odf(path, "odp"), image_prefix="odp-image")
        except OdfError as exc:
            logger.warning("Failed to read ODP: %s", exc)
            return 0, [], []
