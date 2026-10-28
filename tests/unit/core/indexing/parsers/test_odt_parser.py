"""Unit tests for :class:`OdtParser` (#1287)."""

from __future__ import annotations

import io
import zipfile

import pytest
from core.indexing.parsers.odf import _MAX_REPEATED_CHARS, _MAX_TABLE_CELLS
from core.indexing.parsers.odt_parser import OdtParser
from core.models.document import Document, DocumentType


async def _parse(raw: bytes):
    return await OdtParser().parse(Document(filename="x.odt", content_type=DocumentType.ODT, raw_bytes=raw))


async def _markdown(odf_package, body: str, **kwargs) -> str:
    processed = await _parse(odf_package("odt", body, **kwargs))
    assert len(processed.text_blocks) == 1
    return processed.text_blocks[0].text


@pytest.mark.asyncio
async def test_headings_and_paragraphs(odf_package):
    md = await _markdown(
        odf_package,
        '<text:h text:outline-level="1">Rapport annuel</text:h>'
        "<text:p>Premier paragraphe.</text:p>"
        '<text:h text:outline-level="2">Budget</text:h>'
        "<text:p>Deux<text:s/>espaces, <text:span>un span</text:span>, et<text:tab/>une tabulation.</text:p>",
    )

    assert md == ("# Rapport annuel\n\nPremier paragraphe.\n\n## Budget\n\nDeux espaces, un span, et\tune tabulation.")


@pytest.mark.asyncio
async def test_repeated_spaces_and_line_breaks(odf_package):
    md = await _markdown(odf_package, '<text:p>a<text:s text:c="3"/>b<text:line-break/>c</text:p>')

    assert md == "a   b\nc"


@pytest.mark.asyncio
async def test_bulleted_and_numbered_lists_follow_their_style(odf_package):
    styles = (
        '<text:list-style style:name="Numbered">'
        '<text:list-level-style-number text:level="1" style:num-format="1"/>'
        '<text:list-level-style-bullet text:level="2" text:bullet-char="-"/>'
        "</text:list-style>"
    )
    md = await _markdown(
        odf_package,
        '<text:list text:style-name="Numbered">'
        "<text:list-item><text:p>Un</text:p>"
        "<text:list><text:list-item><text:p>sous-point</text:p></text:list-item></text:list>"
        "</text:list-item>"
        "<text:list-item><text:p>Deux</text:p></text:list-item>"
        "</text:list>"
        "<text:list><text:list-item><text:p>Puce</text:p></text:list-item></text:list>",
        automatic_styles=styles,
    )

    assert md == "1. Un\n  - sous-point\n2. Deux\n\n- Puce"


@pytest.mark.asyncio
async def test_list_styles_are_also_read_from_styles_xml(odf_package):
    md = await _markdown(
        odf_package,
        '<text:list text:style-name="Named"><text:list-item><text:p>Un</text:p></text:list-item></text:list>',
        styles=(
            '<text:list-style style:name="Named">'
            '<text:list-level-style-number text:level="1" style:num-format="1"/></text:list-style>'
        ),
    )

    assert md == "1. Un"


@pytest.mark.asyncio
async def test_tables_become_markdown_tables(odf_package):
    md = await _markdown(
        odf_package,
        "<table:table>"
        "<table:table-header-rows><table:table-row>"
        "<table:table-cell><text:p>Ville</text:p></table:table-cell>"
        "<table:table-cell><text:p>Habitants</text:p></table:table-cell>"
        "</table:table-row></table:table-header-rows>"
        "<table:table-row>"
        "<table:table-cell><text:p>Paris</text:p></table:table-cell>"
        "<table:table-cell><text:p>2 100 000</text:p></table:table-cell>"
        "</table:table-row>"
        "</table:table>",
    )

    assert "Ville" in md and "Habitants" in md
    assert "| Paris | 2 100 000 |" in md
    assert "---" in md


@pytest.mark.asyncio
async def test_repeated_empty_columns_and_rows_are_not_expanded(odf_package):
    """Spreadsheet-style tables declare 1024 repeated empty columns."""
    md = await _markdown(
        odf_package,
        "<table:table>"
        "<table:table-row>"
        "<table:table-cell><text:p>A</text:p></table:table-cell>"
        '<table:table-cell table:number-columns-repeated="1024"/>'
        "</table:table-row>"
        '<table:table-row table:number-rows-repeated="1048576"><table:table-cell/></table:table-row>'
        "</table:table>",
    )

    assert md.count("|") <= 4


@pytest.mark.asyncio
async def test_notes_annotations_tracked_deletions_and_tocs_are_left_out(odf_package):
    md = await _markdown(
        odf_package,
        "<text:tracked-changes><text:changed-region><text:deletion>"
        "<text:p>texte supprimé</text:p></text:deletion></text:changed-region></text:tracked-changes>"
        "<text:table-of-content><text:index-body><text:p>Sommaire</text:p></text:index-body></text:table-of-content>"
        "<text:p>Corps<text:note><text:note-body><text:p>note de bas de page</text:p></text:note-body></text:note>"
        "<office:annotation><text:p>commentaire</text:p></office:annotation> du texte.</text:p>",
    )

    assert md == "Corps du texte."


@pytest.mark.asyncio
async def test_sections_are_walked(odf_package):
    md = await _markdown(odf_package, "<text:section><text:p>Dans une section</text:p></text:section>")

    assert md == "Dans une section"


@pytest.mark.asyncio
async def test_pictures_become_refs_with_image_blocks(odf_package, encoded_image):
    processed = await _parse(
        odf_package(
            "odt",
            "<text:p>Avant "
            '<draw:frame><draw:image xlink:href="Pictures/a.png"/></draw:frame>'
            " après</text:p>"
            '<text:p><draw:frame><draw:image xlink:href="Pictures/missing.png"/></draw:frame></text:p>',
            pictures={"Pictures/a.png": encoded_image("PNG")},
        )
    )

    assert processed.text_blocks[0].text == "Avant ![](odt-image-0) après"
    assert len(processed.images) == 1
    image = processed.images[0]
    assert image.metadata["markdown_ref"] == "![](odt-image-0)"
    assert image.mime_type == "image/png"
    assert image.image_bytes.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_a_picture_falls_back_to_its_first_decodable_alternative(odf_package, encoded_image):
    processed = await _parse(
        odf_package(
            "odt",
            "<text:p><draw:frame>"
            '<draw:image xlink:href="Pictures/a.svm"/><draw:image xlink:href="Pictures/a.png"/>'
            "</draw:frame></text:p>",
            pictures={"Pictures/a.svm": b"VCLMTF not an image Pillow reads", "Pictures/a.png": encoded_image("PNG")},
        )
    )

    assert processed.text_blocks[0].text == "![](odt-image-0)"
    assert len(processed.images) == 1


@pytest.mark.asyncio
async def test_external_and_undecodable_pictures_leave_no_ref(odf_package):
    processed = await _parse(
        odf_package(
            "odt",
            '<text:p>a<draw:frame><draw:image xlink:href="https://example.com/x.png"/></draw:frame>'
            '<draw:frame><draw:image xlink:href="Pictures/x.wmf"/></draw:frame>b</text:p>',
            pictures={"Pictures/x.wmf": b"\xd7\xcd\xc6\x9a not really wmf"},
        )
    )

    assert processed.text_blocks[0].text == "ab"
    assert processed.images == []


@pytest.mark.asyncio
async def test_external_entities_are_not_resolved(odf_package, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    raw = odf_package(
        "odt",
        "<text:p>avant &xxe; après</text:p>",
        doctype=f'<!DOCTYPE office:document-content [<!ENTITY xxe SYSTEM "file://{secret}">]>',
    )

    processed = await _parse(raw)

    assert all("TOP SECRET" not in block.text for block in processed.text_blocks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"not a zip", id="not-a-zip"),
        pytest.param(b"PK\x05\x06" + b"\x00" * 18, id="empty-zip"),
    ],
)
async def test_unreadable_packages_index_as_nothing(raw):
    processed = await _parse(raw)

    assert processed.text_blocks == []
    assert processed.page_count == 0


@pytest.mark.asyncio
async def test_malformed_content_xml_indexes_as_nothing():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", "<office:document-content><unclosed>")

    processed = await _parse(out.getvalue())

    assert processed.text_blocks == []


@pytest.mark.asyncio
async def test_empty_document_has_no_block():
    processed = await OdtParser().parse(Document(filename="x.odt", content_type=DocumentType.ODT, raw_bytes=b""))

    assert processed.text_blocks == []


# ---------------------------------------------------------------------------
# Declared counts must not make output out of nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_huge_space_count_is_bounded(odf_package):
    md = await _markdown(odf_package, '<text:p>a<text:s text:c="999999999"/>b</text:p>')

    assert len(md) <= _MAX_REPEATED_CHARS + 2


@pytest.mark.asyncio
async def test_space_counts_share_one_budget_across_the_document(odf_package):
    spaces = '<text:s text:c="1000000"/>' * 50
    md = await _markdown(odf_package, f"<text:p>a{spaces}b</text:p>")

    assert len(md) <= _MAX_REPEATED_CHARS + 50 + 2


@pytest.mark.asyncio
async def test_repeated_cells_and_rows_with_content_are_bounded(odf_package):
    cell = f'<table:table-cell table:number-columns-repeated="1024"><text:p>{"x" * 1000}</text:p></table:table-cell>'
    md = await _markdown(
        odf_package,
        f'<table:table><table:table-row table:number-rows-repeated="1048576">{cell}</table:table-row></table:table>',
    )

    assert len(md) < 3 * _MAX_REPEATED_CHARS


@pytest.mark.asyncio
async def test_nested_tables_are_read_once(odf_package):
    """Walking every descendant row made each nesting level re-read the levels
    below it: 2^depth work for a file a few kilobytes long."""
    body = "<text:p>fond</text:p>"
    for _ in range(40):
        body = (
            "<table:table><table:table-row>"
            f"<table:table-cell>{body}</table:table-cell>"
            "<table:table-cell><text:p>x</text:p></table:table-cell>"
            "</table:table-row></table:table>"
        )

    md = await _markdown(odf_package, body)

    assert md.count("fond") == 1


@pytest.mark.asyncio
async def test_padding_short_rows_to_a_wide_one_is_bounded(odf_package):
    """One 256-column row makes every other row pad to 256 cells."""
    wide = "<table:table-row>" + "<table:table-cell><text:p>w</text:p></table:table-cell>" * 256 + "</table:table-row>"
    short = '<table:table-row table:number-rows-repeated="10000"><table:table-cell><text:p>x</text:p></table:table-cell></table:table-row>'
    tables = f"<table:table>{wide}{short}</table:table>" * 50

    md = await _markdown(odf_package, tables)

    assert md.count("|") <= 2 * (_MAX_TABLE_CELLS + 50 * 256)
