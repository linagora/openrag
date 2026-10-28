"""Unit tests for :class:`OdpParser` (#1287)."""

from __future__ import annotations

import pytest
from core.indexing.parsers.odp_parser import OdpParser
from core.models.document import Document, DocumentType


async def _parse(raw: bytes):
    return await OdpParser().parse(Document(filename="x.odp", content_type=DocumentType.ODP, raw_bytes=raw))


def _frame(shape_class: str, content: str) -> str:
    return f'<draw:frame presentation:class="{shape_class}"><draw:text-box>{content}</draw:text-box></draw:frame>'


def _notes(text: str) -> str:
    return (
        f"<presentation:notes><draw:page-thumbnail/>{_frame('notes', f'<text:p>{text}</text:p>')}</presentation:notes>"
    )


@pytest.mark.asyncio
async def test_one_block_per_slide_with_title_body_and_notes(odf_package):
    processed = await _parse(
        odf_package(
            "odp",
            '<draw:page draw:name="p1">'
            f"{_frame('title', '<text:p>Introduction</text:p>')}"
            f"{
                _frame(
                    'outline',
                    '<text:list><text:list-item><text:p>Contexte</text:p></text:list-item>'
                    '<text:list-item><text:p>Objectifs</text:p></text:list-item></text:list>',
                )
            }"
            f"{_notes('Rappeler le calendrier.')}"
            "</draw:page>"
            '<draw:page draw:name="p2">'
            f"{_frame('title', '<text:p>Conclusion</text:p>')}"
            f"{_frame('subtitle', '<text:p>Merci</text:p>')}"
            "</draw:page>",
        )
    )

    assert processed.page_count == 2
    assert [(block.page_number, block.text) for block in processed.text_blocks] == [
        (1, "# Introduction\n- Contexte\n- Objectifs\n\n### Notes:\nRappeler le calendrier."),
        (2, "# Conclusion\nMerci"),
    ]


@pytest.mark.asyncio
async def test_an_empty_slide_is_skipped_but_counted(odf_package):
    processed = await _parse(
        odf_package(
            "odp",
            f"<draw:page/><draw:page>{_frame('title', '<text:p>Deuxième</text:p>')}</draw:page>",
        )
    )

    assert processed.page_count == 2
    assert [(block.page_number, block.text) for block in processed.text_blocks] == [(2, "# Deuxième")]


@pytest.mark.asyncio
async def test_footer_page_number_and_date_placeholders_are_left_out(odf_package):
    processed = await _parse(
        odf_package(
            "odp",
            "<draw:page>"
            f"{_frame('title', '<text:p>Titre</text:p>')}"
            f"{_frame('footer', '<text:p>Confidentiel</text:p>')}"
            f"{_frame('page-number', '<text:p>3</text:p>')}"
            f"{_frame('date-time', '<text:p>28/10/2026</text:p>')}"
            "</draw:page>",
        )
    )

    assert processed.text_blocks[0].text == "# Titre"


@pytest.mark.asyncio
async def test_custom_shapes_groups_and_tables_keep_their_text(odf_package):
    processed = await _parse(
        odf_package(
            "odp",
            "<draw:page>"
            "<draw:custom-shape><text:p>Dans une forme</text:p></draw:custom-shape>"
            "<draw:g><draw:rect><text:p>Dans un groupe</text:p></draw:rect></draw:g>"
            "<draw:frame><table:table>"
            "<table:table-row><table:table-cell><text:p>Q1</text:p></table:table-cell>"
            "<table:table-cell><text:p>Q2</text:p></table:table-cell></table:table-row>"
            "<table:table-row><table:table-cell><text:p>10</text:p></table:table-cell>"
            "<table:table-cell><text:p>20</text:p></table:table-cell></table:table-row>"
            "</table:table></draw:frame>"
            "</draw:page>",
        )
    )

    text = processed.text_blocks[0].text
    assert text.startswith("Dans une forme\nDans un groupe\n")
    assert "| 10 | 20 |" in text


@pytest.mark.asyncio
async def test_pictures_carry_their_slide_number(odf_package, encoded_image):
    picture = '<draw:frame><draw:image xlink:href="Pictures/chart.png"/></draw:frame>'
    processed = await _parse(
        odf_package(
            "odp",
            f"<draw:page>{_frame('title', '<text:p>Un</text:p>')}</draw:page>"
            f"<draw:page>{_frame('title', '<text:p>Deux</text:p>')}{picture}{picture}</draw:page>",
            pictures={"Pictures/chart.png": encoded_image("PNG")},
        )
    )

    assert processed.text_blocks[1].text == "# Deux\n![](odp-image-0)\n![](odp-image-1)"
    assert [(image.page_number, image.metadata["markdown_ref"]) for image in processed.images] == [
        (2, "![](odp-image-0)"),
        (2, "![](odp-image-1)"),
    ]


@pytest.mark.asyncio
async def test_a_text_document_is_not_read_as_a_presentation(odf_package):
    processed = await _parse(odf_package("odt", "<text:p>Pas une présentation</text:p>"))

    assert processed.text_blocks == []
    assert processed.page_count == 0


@pytest.mark.asyncio
async def test_unreadable_package_indexes_as_nothing():
    processed = await _parse(b"not a zip")

    assert processed.text_blocks == []
    assert processed.page_count == 0
