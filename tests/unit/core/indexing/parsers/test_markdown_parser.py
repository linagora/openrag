from __future__ import annotations

import base64

import pytest
from core.indexing.parsers.markdown_parser import MarkdownParser
from core.models.document import Document, DocumentType


@pytest.mark.asyncio
async def test_markdown_parser_sanitizes_embedded_image_when_captioning_is_not_run():
    encoded = base64.b64encode(b"image-bytes").decode()
    source = f"Before ![chart](data:image/png;base64,{encoded}) after"

    processed = await MarkdownParser().parse(
        Document(filename="report.md", content_type=DocumentType.MARKDOWN, text=source)
    )

    assert len(processed.images) == 1
    assert "data:image" not in processed.text_blocks[0].text
    assert processed.images[0].metadata["markdown_ref"] in processed.text_blocks[0].text


@pytest.mark.asyncio
async def test_markdown_parser_preserves_http_image_behavior():
    processed = await MarkdownParser().parse(
        Document(
            filename="report.md",
            content_type=DocumentType.MARKDOWN,
            text="Before ![chart](https://example.test/chart.png) after",
        )
    )

    assert processed.text_blocks[0].text == "Before ![chart](https://example.test/chart.png) after"
    assert len(processed.images) == 1
    assert processed.images[0].source_url == "https://example.test/chart.png"
