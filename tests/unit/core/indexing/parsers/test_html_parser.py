from __future__ import annotations

import base64
from contextlib import asynccontextmanager

import pytest
from core.indexing.parsers.html_parser import HtmlParser
from core.models.document import Document, DocumentType
from services.workers.stages import caption as caption_module
from services.workers.stages.caption import caption_stage


@pytest.fixture(autouse=True)
async def _noop_vlm_semaphore(monkeypatch):
    """Stub the cluster-wide VLM semaphore so caption tests don't boot Ray."""

    @asynccontextmanager
    async def _noop():
        yield

    monkeypatch.setattr(caption_module, "get_vlm_semaphore", _noop)


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


@pytest.mark.asyncio
async def test_caption_inside_tbody_is_kept():
    html = "<table><tbody><caption>alpha beta</caption><tr><td>cell</td></tr></tbody></table>"

    processed = await HtmlParser().parse(
        Document(
            filename="u.html",
            content_type=DocumentType.HTML,
            raw_bytes=html.encode("utf-8"),
        )
    )

    text = processed.text_blocks[0].text

    assert "cell" in text
    assert "alpha" in text and "beta" in text


@pytest.mark.asyncio
async def test_html_parser_preserves_plain_paragraph():
    html = "<p>Report caption: alpha beta</p>"

    processed = await HtmlParser().parse(
        Document(
            filename="caption.html",
            content_type=DocumentType.HTML,
            text=html,
        )
    )

    text = processed.text_blocks[0].text

    assert "Report caption: alpha beta" in text


@pytest.mark.asyncio
async def test_html_parser_preserves_valid_table_caption():
    html = "<table><caption>Report summary</caption><tbody><tr><td>alpha</td></tr></tbody></table>"

    processed = await HtmlParser().parse(
        Document(
            filename="caption.html",
            content_type=DocumentType.HTML,
            text=html,
        )
    )

    text = processed.text_blocks[0].text

    assert "Report summary" in text
    assert "alpha" in text


@pytest.mark.asyncio
async def test_html_parser_preserves_escaped_angle_brackets_in_caption():
    html = (
        "<table>"
        "<caption>Table containing &lt;script&gt; as text</caption>"
        "<tbody><tr><td>unique-caption-cell</td></tr></tbody>"
        "</table>"
    )

    processed = await HtmlParser().parse(
        Document(
            filename="escaped-caption.html",
            content_type=DocumentType.HTML,
            text=html,
        )
    )

    text = processed.text_blocks[0].text

    assert "Table containing <script> as text" in text
    assert "unique-caption-cell" in text


@pytest.mark.asyncio
async def test_html_parser_preserves_literal_image_attributes_with_caption():
    html = (
        "<table>"
        "<caption>Image table</caption>"
        "<tbody><tr><td>"
        '<img alt="été" src="https://example.com/image.png?a=1&b=2">'
        "</td></tr></tbody>"
        "</table>"
    )

    processed = await HtmlParser().parse(
        Document(
            filename="image-caption.html",
            content_type=DocumentType.HTML,
            text=html,
        )
    )

    text = processed.text_blocks[0].text

    assert "été" in text
    assert "&eacute;" not in text
    assert "a=1&amp;b=2" in text


@pytest.mark.asyncio
async def test_html_parser_preserves_content_around_table_with_bare_col():
    html = (
        "<p>T\u00e9l\u00e9chargez le rapport</p>"
        "<table><colgroup><col></colgroup>"
        "<tbody><caption>Table summary</caption>"
        "<tr><td>unique-cell-value</td></tr></tbody></table>"
        "<p>apr\u00e8s</p>"
    )

    processed = await HtmlParser().parse(
        Document(
            filename="col.html",
            content_type=DocumentType.HTML,
            raw_bytes=html.encode("utf-8"),
        )
    )

    text = processed.text_blocks[0].text

    assert "T\u00e9l\u00e9chargez le rapport" in text
    assert "Table summary" in text
    assert "unique-cell-value" in text
    assert "apr\u00e8s" in text
@pytest.mark.asyncio
async def test_html_parser_preserves_unicode_attribute_values_with_caption():
    html = (
        "<table><caption>Résumé</caption><tbody>"
        "<tr><td>"
        '<abbr title="Société">Sté</abbr>, '
        '<a href="/x" title="L’été">lien</a>'
        "</td></tr></tbody></table>"
    )

    processed = await HtmlParser().parse(
        Document(
            filename="unicode-attributes.html",
            content_type=DocumentType.HTML,
            text=html,
        )
    )

    text = processed.text_blocks[0].text

    assert "Sté (Société)" in text
    assert '[lien](/x "L’été")' in text
