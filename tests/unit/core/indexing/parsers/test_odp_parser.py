"""Unit tests for :class:`OdpParser` (#1287)."""

from __future__ import annotations

import io
import zipfile
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
        (1, "# Introduction\n\n- Contexte\n- Objectifs\n\n### Notes:\nRappeler le calendrier."),
        (2, "# Conclusion\n\nMerci"),
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
    assert text.startswith("Dans une forme\n\nDans un groupe\n\n")
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

    assert processed.text_blocks[1].text == "# Deux\n\n![](odp-image-0)\n\n![](odp-image-1)"
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
@pytest.mark.parametrize(
    ("shape", "node"),
    [("custom-shape", "<!-- commentaire -->"), ("rect", "<?instruction x?>")],
)
async def test_a_shape_holding_a_non_element_node_keeps_its_text(odf_package, shape, node):
    processed = await _parse(
        odf_package(
            "odp",
            f"<draw:page><draw:{shape}>{node}<text:p>Dans une forme</text:p></draw:{shape}></draw:page>",
        )
    )

    assert "Dans une forme" in processed.text_blocks[0].text


@pytest.mark.asyncio
async def test_an_entity_reference_indexes_as_nothing(odf_package):
    """xberg refuses the entity, so it is the file that is lost, not the indexing task."""
    processed = await _parse(
        odf_package(
            "odp",
            "<draw:page><draw:ellipse>&entite;<text:p>Dans une forme</text:p></draw:ellipse></draw:page>",
            doctype='<!DOCTYPE x [<!ENTITY entite "v">]>',
        )
    )

    assert processed.text_blocks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("root", ["text:s", "text:list-header"])
async def test_content_xml_that_is_not_a_document_indexes_as_nothing(root):
    """The patches assume elements have parents: these fail on the root, without failing the task."""
    namespaces = (
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as package:
        package.writestr("mimetype", "application/vnd.oasis.opendocument.presentation")
        package.writestr("content.xml", f"<{root} {namespaces}/>")
        package.writestr("META-INF/manifest.xml", "<manifest/>")

    processed = await _parse(buf.getvalue())

    assert processed.text_blocks == []


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
    assert text.startswith("# Évolution\n\n### Chart: Ventes\n\n")
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
async def test_frames_naming_one_chart_each_get_its_table(odf_package):
    processed = await _parse(
        odf_package(
            "odp",
            f"<draw:page>{_CHART_FRAME * 3}</draw:page>",
            pictures={"Object 1/content.xml": _CHART_OBJECT.encode()},
        )
    )

    assert processed.text_blocks[0].text.count("### Chart: Ventes") == 3


@pytest.mark.asyncio
async def test_what_charts_add_is_bounded(odf_package, monkeypatch):
    """Every frame may name the same large table: a small file must not multiply it."""
    import core.indexing.parsers.odf as odf

    monkeypatch.setattr(odf, "_MAX_CHART_ELEMENTS", 40)
    processed = await _parse(
        odf_package(
            "odp",
            f"<draw:page>{_CHART_FRAME * 5}</draw:page>",
            pictures={"Object 1/content.xml": _CHART_OBJECT.encode()},
        )
    )

    assert processed.text_blocks[0].text.count("### Chart: Ventes") < 5


@pytest.mark.asyncio
async def test_a_title_written_as_a_list_header_is_kept(odf_package):
    title = "<text:list><text:list-header><text:p>Agenda</text:p></text:list-header></text:list>"
    processed = await _parse(odf_package("odp", f"<draw:page>{_frame('title', title)}</draw:page>"))

    assert processed.text_blocks[0].text == "# Agenda"


@pytest.mark.asyncio
async def test_a_slide_with_no_title_does_not_take_its_name_for_one(odf_package):
    processed = await _parse(
        odf_package("odp", f'<draw:page draw:name="page4">{_frame("outline", "<text:p>Corps</text:p>")}</draw:page>')
    )

    assert processed.text_blocks[0].text == "Corps"


@pytest.mark.asyncio
async def test_notes_holding_a_list_are_kept(odf_package):
    notes = (
        '<presentation:notes><draw:page-thumbnail/><draw:frame presentation:class="notes"><draw:text-box>'
        "<text:list><text:list-item><text:p>Point oral</text:p></text:list-item></text:list>"
        "</draw:text-box></draw:frame></presentation:notes>"
    )
    processed = await _parse(
        odf_package("odp", f"<draw:page>{_frame('title', '<text:p>T</text:p>')}{notes}</draw:page>")
    )

    assert processed.text_blocks[0].text == "# T\n\n### Notes:\nPoint oral"


@pytest.mark.asyncio
async def test_a_note_repeating_the_title_leaves_the_title_alone(odf_package):
    processed = await _parse(
        odf_package(
            "odp",
            "<draw:page>"
            f"{_frame('title', '<text:p>Budget</text:p>')}"
            f"{_frame('outline', '<text:p>Budget 2024 review</text:p>')}"
            f"{_notes('Budget')}"
            "</draw:page>",
        )
    )

    assert processed.text_blocks[0].text == "# Budget\n\nBudget 2024 review\n\n### Notes:\nBudget"


# ---------------------------------------------------------------------------
# A deck LibreOffice wrote (26.8, exported from a python-pptx deck)
# ---------------------------------------------------------------------------

_RESOURCES = Path(__file__).resolve().parents[4] / "resources"


@pytest.mark.asyncio
async def test_a_libreoffice_deck():
    processed = await _parse((_RESOURCES / "test_file.odp").read_bytes())

    assert processed.page_count == 6
    assert [(block.page_number, block.text) for block in processed.text_blocks] == [
        (1, "# Bilan 2026\n\nDirection des finances\n\n### Notes:\nRemercier l'équipe."),
        (2, "# Objectifs\n\n- Réduire les coûts\n\n- - Énergie\n\n- Améliorer le service"),
        (3, "# Chiffres clés\n\n| Ville | Budget |\n| --- | --- |\n| Paris | 1 200 |\n| Lyon | 640 |"),
        (4, "![](odp-image-0)\n\nPhoto du chantier\n\n### Notes:\nMontrer la photo."),
        (5, "# Évolution\n\n### Chart: Ventes\n\n|  | Ventes |\n| --- | --- |\n| T1 | 10 |\n| T2 | 20 |"),
    ]
    # The table's preview picture is not mistaken for a picture of the slide.
    assert [(image.page_number, image.metadata["markdown_ref"]) for image in processed.images] == [
        (4, "![](odp-image-0)")
    ]
