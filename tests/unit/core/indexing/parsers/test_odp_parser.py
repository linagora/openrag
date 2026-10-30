"""Unit tests for :class:`OdpParser` (#1287)."""

from __future__ import annotations

from pathlib import Path

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


_CHART_OBJECT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:chart="urn:oasis:names:tc:opendocument:xmlns:chart:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
    "<office:body><office:chart><chart:chart>"
    "<chart:title><text:p>Ventes</text:p></chart:title>"
    "<table:table><table:table-header-rows><table:table-row>"
    "<table:table-cell/><table:table-cell><text:p>2026</text:p></table:table-cell>"
    "</table:table-row></table:table-header-rows>"
    "<table:table-rows><table:table-row>"
    "<table:table-cell><text:p>T1</text:p></table:table-cell><table:table-cell><text:p>10</text:p></table:table-cell>"
    "</table:table-row></table:table-rows></table:table>"
    "</chart:chart></office:chart></office:body></office:document-content>"
)
_CHART_FRAME = (
    '<draw:frame><draw:object xlink:href="./Object 1"/>'
    '<draw:image xlink:href="./ObjectReplacements/Object 1"/></draw:frame>'
)


@pytest.mark.asyncio
async def test_a_chart_becomes_its_title_and_data_table(odf_package):
    processed = await _parse(
        odf_package(
            "odp",
            f"<draw:page>{_frame('title', '<text:p>Évolution</text:p>')}{_CHART_FRAME}</draw:page>",
            pictures={"Object 1/content.xml": _CHART_OBJECT.encode(), "ObjectReplacements/Object 1": b"SVM"},
        )
    )

    text = processed.text_blocks[0].text
    assert text.startswith("# Évolution\n### Chart: Ventes\n\n")
    assert "| T1 | 10 |" in text
    assert processed.images == []


@pytest.mark.asyncio
async def test_an_object_that_is_not_a_chart_leaves_nothing(odf_package):
    formula = (
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0">'
        "<office:body><office:formula/></office:body></office:document-content>"
    )
    processed = await _parse(
        odf_package(
            "odp",
            f"<draw:page>{_frame('title', '<text:p>Formule</text:p>')}{_CHART_FRAME}</draw:page>",
            pictures={"Object 1/content.xml": formula.encode()},
        )
    )

    assert processed.text_blocks[0].text == "# Formule"


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["encrypted", "corrupt"])
async def test_an_unreadable_chart_is_skipped(odf_package, damaged_zip_entry, how):
    package = odf_package(
        "odp",
        f"<draw:page>{_frame('title', '<text:p>Évolution</text:p>')}{_CHART_FRAME}</draw:page>",
        pictures={"Object 1/content.xml": _CHART_OBJECT.encode()},
    )
    processed = await _parse(damaged_zip_entry(package, "Object 1/content.xml", how))

    assert processed.text_blocks[0].text == "# Évolution"


@pytest.mark.asyncio
async def test_charts_share_one_xml_budget(odf_package, monkeypatch):
    """Every frame may name the same object: what they read is budgeted."""
    import core.indexing.parsers.odf as odf

    monkeypatch.setattr(odf, "_MAX_OBJECT_XML_BYTES", 2 * len(_CHART_OBJECT))
    processed = await _parse(
        odf_package(
            "odp",
            f"<draw:page>{_CHART_FRAME * 5}</draw:page>",
            pictures={"Object 1/content.xml": _CHART_OBJECT.encode()},
        )
    )

    assert processed.text_blocks[0].text.count("### Chart: Ventes") == 2


# ---------------------------------------------------------------------------
# A deck LibreOffice wrote (26.8, exported from a python-pptx deck)
# ---------------------------------------------------------------------------

_RESOURCES = Path(__file__).resolve().parents[4] / "resources"


@pytest.mark.asyncio
async def test_a_libreoffice_deck():
    processed = await _parse((_RESOURCES / "test_file.odp").read_bytes())

    assert processed.page_count == 6
    assert [(block.page_number, block.text) for block in processed.text_blocks] == [
        (1, "# Bilan 2026\nDirection des finances\n\n### Notes:\nRemercier l'équipe."),
        (2, "# Objectifs\n- Réduire les coûts\n  - Énergie\n- Améliorer le service"),
        (3, "# Chiffres clés\n| Ville | Budget |\n| --- | --- |\n| Paris | 1 200 |\n| Lyon | 640 |"),
        (4, "![](odp-image-0)\nPhoto du chantier\n\n### Notes:\nMontrer la photo."),
        (5, "# Évolution\n### Chart: Ventes\n\n|  | Ventes |\n| --- | --- |\n| T1 | 10 |\n| T2 | 20 |"),
    ]
    # The table's preview picture is not mistaken for a picture of the slide.
    assert [(image.page_number, image.metadata["markdown_ref"]) for image in processed.images] == [
        (4, "![](odp-image-0)")
    ]
