from __future__ import annotations

import base64

import pytest
from core.indexing.parsers.html_parser import HtmlParser
from core.models.document import Document, DocumentType
from services.workers.stages.caption import caption_stage


def _html_with_image(payload: bytes = b"image-bytes") -> str:
    encoded = base64.b64encode(payload).decode()
    return f'<p>Report</p><img alt="chart" src="data:image/png;base64,{encoded}"><p>End</p>'


@pytest.mark.asyncio
async def test_html_parser_extracts_embedded_images_without_leaking_base64():
    processed = await HtmlParser().parse(
        Document(filename="report.html", content_type=DocumentType.HTML, text=_html_with_image())
    )

    assert len(processed.images) == 1
    assert processed.images[0].image_bytes == b"image-bytes"
    assert processed.images[0].metadata["alt"] == "chart"
    assert "data:image" not in processed.text_blocks[0].text
    assert processed.images[0].metadata["markdown_ref"] in processed.text_blocks[0].text


@pytest.mark.asyncio
async def test_html_image_placeholder_is_replaced_by_caption():
    class FakeVLM:
        async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
            assert image_bytes == b"image-bytes"
            return "Quarterly sales chart"

    processed = await HtmlParser().parse(
        Document(filename="report.html", content_type=DocumentType.HTML, text=_html_with_image())
    )
    row = {"processed_document": processed}

    await caption_stage(row, FakeVLM())

    text = row["processed_document"].text_blocks[0].text
    assert "Quarterly sales chart" in text
    assert "openrag-embedded-image" not in text
    assert "data:image" not in text


@pytest.mark.asyncio
async def test_html_without_images_is_unchanged():
    processed = await HtmlParser().parse(
        Document(filename="plain.html", content_type=DocumentType.HTML, text="<p>Hello <strong>world</strong></p>")
    )

    assert processed.images == []
    assert processed.text_blocks[0].text == "Hello **world**"


@pytest.mark.asyncio
async def test_html_parser_handles_parameterized_data_uri():
    encoded = base64.b64encode(b"svg-image").decode()
    html = f'<img alt="diagram" src="data:image/svg+xml;charset=utf-8;base64,{encoded}">'

    processed = await HtmlParser().parse(Document(filename="diagram.html", content_type=DocumentType.HTML, text=html))

    assert "data:image" not in processed.text_blocks[0].text
    assert len(processed.images) == 1
    assert processed.images[0].mime_type == "image/svg+xml"


@pytest.mark.asyncio
async def test_html_parser_handles_an_embedded_image_with_a_title():
    encoded = base64.b64encode(b"logo-image").decode()
    html = f'<img alt="logo" title="Company logo" src="data:image/png;base64,{encoded}">'

    processed = await HtmlParser().parse(Document(filename="logo.html", content_type=DocumentType.HTML, text=html))

    assert "data:image" not in processed.text_blocks[0].text
    assert len(processed.images) == 1
    assert processed.images[0].image_bytes == b"logo-image"


@pytest.mark.asyncio
async def test_html_parser_sanitizes_data_uri_links():
    encoded = base64.b64encode(b"logo-image").decode()
    html = f'<a href="data:image/png;base64,{encoded}">logo</a>'

    processed = await HtmlParser().parse(Document(filename="link.html", content_type=DocumentType.HTML, text=html))

    assert processed.text_blocks[0].text == "logo"
    assert processed.images == []
