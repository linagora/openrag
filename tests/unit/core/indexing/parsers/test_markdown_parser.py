from __future__ import annotations

import base64

import pytest
from core.indexing.parsers.markdown_parser import MarkdownParser
from core.models.document import Document, DocumentType
from services.workers.stages.caption import caption_stage


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


@pytest.mark.asyncio
async def test_markdown_parser_sanitizes_data_uri_links():
    encoded = base64.b64encode(b"image-bytes").decode()
    source = f"See [the logo](data:image/png;base64,{encoded}) here"

    processed = await MarkdownParser().parse(
        Document(filename="report.md", content_type=DocumentType.MARKDOWN, text=source)
    )

    assert processed.text_blocks[0].text == "See the logo here"
    assert processed.images == []


@pytest.mark.asyncio
async def test_markdown_parser_sanitizes_angle_bracketed_data_uri_images():
    encoded = base64.b64encode(b"image-bytes").decode()
    source = f"Before ![chart](<data:image/png;base64,{encoded}>) after"

    processed = await MarkdownParser().parse(
        Document(filename="report.md", content_type=DocumentType.MARKDOWN, text=source)
    )

    assert "data:image" not in processed.text_blocks[0].text
    assert len(processed.images) == 1
    assert processed.images[0].image_bytes == b"image-bytes"


@pytest.mark.asyncio
async def test_markdown_parser_sanitizes_reference_style_data_uri_images():
    encoded = base64.b64encode(b"image-bytes").decode()
    source = f"Before ![logo][asset] after\n\n[asset]: data:image/png;base64,{encoded}"

    processed = await MarkdownParser().parse(
        Document(filename="report.md", content_type=DocumentType.MARKDOWN, text=source)
    )

    assert "data:image" not in processed.text_blocks[0].text
    assert "[asset]:" not in processed.text_blocks[0].text
    assert len(processed.images) == 1
    assert processed.images[0].image_bytes == b"image-bytes"


@pytest.mark.asyncio
async def test_markdown_parser_sanitizes_raw_html_data_uri_images():
    encoded = base64.b64encode(b"image-bytes").decode()
    source = f'Before <img alt="chart" src="data:image/png;base64,{encoded}"> after'

    processed = await MarkdownParser().parse(
        Document(filename="report.md", content_type=DocumentType.MARKDOWN, text=source)
    )

    assert "data:image" not in processed.text_blocks[0].text.lower()
    assert encoded not in processed.text_blocks[0].text
    assert processed.images == []


@pytest.mark.asyncio
async def test_markdown_parser_sanitizes_unterminated_data_uri_images():
    encoded = base64.b64encode(b"image-bytes").decode()
    source = f"Before ![chart](data:image/png;base64,{encoded}"

    processed = await MarkdownParser().parse(
        Document(filename="report.md", content_type=DocumentType.MARKDOWN, text=source)
    )

    assert "data:image" not in processed.text_blocks[0].text.lower()
    assert encoded not in processed.text_blocks[0].text
    assert processed.images == []


@pytest.mark.asyncio
async def test_captioning_does_not_replace_a_preexisting_similar_reference():
    class FakeVLM:
        async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
            return "Generated caption"

    encoded = base64.b64encode(b"image-bytes").decode()
    existing = "![chart](openrag-embedded-image-1)"
    source = f"{existing}\n\n![chart](data:image/png;base64,{encoded})"
    processed = await MarkdownParser().parse(
        Document(filename="report.md", content_type=DocumentType.MARKDOWN, text=source)
    )
    row = {"processed_document": processed}

    await caption_stage(row, FakeVLM())

    text = row["processed_document"].text_blocks[0].text
    assert existing in text
    assert "Generated caption" in text
