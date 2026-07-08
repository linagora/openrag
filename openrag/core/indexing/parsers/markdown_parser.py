"""Markdown ``DocumentParser`` implementation.

Decodes a Markdown document into a single :class:`TextBlock` and emits
one :class:`ImageBlock` per image reference in the source:

- Data-URI refs (``![alt](data:image/...;base64,...)``) are decoded and
  the bytes stored on the block.
- HTTP/HTTPS refs (``![alt](https://...)``) yield an :class:`ImageBlock`
  with empty ``image_bytes`` and ``source_url`` set; a downstream fetch
  stage can populate the bytes later. The :attr:`ImageBlock.image_url`
  property gives a uniform VLM-friendly URL in either case.

Captioning is not done here — see :class:`ImageBlock` for the
parser→caption contract.
"""

from __future__ import annotations

from ...models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from ..image_preprocessor import HTTP_IMAGE_PATTERN, extract_data_uri_image_blocks
from ..text_preprocessor import decode_bytes
from .document_parser import DocumentParser
from .registry import parser_registry


@parser_registry.register("markdown")
class MarkdownParser(DocumentParser):
    """Parse Markdown documents and emit ImageBlocks for every image ref."""

    def __init__(self, *, encoding: str | None = None) -> None:
        self._encoding = encoding

    def supported_types(self) -> list[str]:
        return [DocumentType.MARKDOWN.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        text = self._extract_text(document).strip()
        images = self._extract_image_blocks(text)

        text_blocks = [TextBlock(text=text, page_number=1)] if text else []
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=text_blocks,
            images=images,
            metadata=dict(document.metadata),
            page_count=1 if text else 0,
        )

    def _extract_text(self, document: Document) -> str:
        if document.text is not None:
            return document.text
        if document.raw_bytes:
            return decode_bytes(document.raw_bytes, encoding=self._encoding)
        return ""

    @staticmethod
    def _extract_image_blocks(text: str) -> list[ImageBlock]:
        if not text:
            return []
        blocks: list[ImageBlock] = list(extract_data_uri_image_blocks(text, page_number=1))
        for alt, url in HTTP_IMAGE_PATTERN.findall(text):
            blocks.append(
                ImageBlock(
                    source_url=url,
                    page_number=1,
                    metadata={"markdown_ref": f"![{alt}]({url})", "alt": alt},
                )
            )
        return blocks
