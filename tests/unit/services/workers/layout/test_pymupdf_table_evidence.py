from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from core.indexing.structure_normalizer import (
    LayoutWord,
    PageLayoutEvidence,
    TableLayoutEvidenceProvider,
)
from core.models.document import Document, DocumentType
from services.workers.layout import PyMuPDFTableEvidenceProvider
from services.workers.layout.pymupdf_table_evidence import (
    _cell_grid_semantics,
    _collect_evidence,
    _text_from_positioned_words,
)

FIXTURE = Path(__file__).parents[4] / "resources" / "cross_page_table_rows_803_805.pdf"


class CollectOnlyEvidenceProvider(TableLayoutEvidenceProvider):
    async def collect(self, document: Document, page_numbers: set[int]) -> list[PageLayoutEvidence]:
        return []


@pytest.mark.asyncio
async def test_provider_discovery_defaults_to_no_candidates():
    document = Document(
        filename="document.pdf",
        content_type=DocumentType.PDF,
        raw_bytes=b"pdf",
    )

    assert await CollectOnlyEvidenceProvider().discover(document) == set()


@pytest.mark.asyncio
async def test_adapter_discovers_table_pages_without_collecting_evidence():
    document = Document(
        filename=FIXTURE.name,
        content_type=DocumentType.PDF,
        raw_bytes=FIXTURE.read_bytes(),
    )

    assert await PyMuPDFTableEvidenceProvider().discover(document) == {1, 3}


@pytest.mark.asyncio
async def test_adapter_exposes_tables_and_sparse_continuation_evidence():
    document = Document(
        filename=FIXTURE.name,
        content_type=DocumentType.PDF,
        raw_bytes=FIXTURE.read_bytes(),
    )

    pages = await PyMuPDFTableEvidenceProvider().collect(document, {1, 2, 3})

    assert [len(page.tables) for page in pages] == [1, 0, 1]
    assert len(pages[0].tables[0].column_bounds) == 5
    page_two_body = [word for word in pages[1].words if word.bbox[3] < 0.90]
    assert page_two_body
    assert min(word.bbox[0] for word in page_two_body) > pages[0].tables[0].column_bounds[-1][0]


@pytest.mark.asyncio
async def test_adapter_repairs_cell_spacing_from_positioned_words():
    document = Document(
        filename=FIXTURE.name,
        content_type=DocumentType.PDF,
        raw_bytes=FIXTURE.read_bytes(),
    )

    pages = await PyMuPDFTableEvidenceProvider().collect(document, {1, 3})
    table_text = "\n".join(
        cell.text for page in pages for table in page.tables for row in table.rows for cell in row.cells
    )
    reflowed = " ".join(table_text.split())

    assert "cas :" in reflowed
    assert "séjour en cours de validité" in reflowed
    assert "d'emploi" in reflowed
    assert "cas :\n\n-visa" in table_text
    for spacing_artifact in ("c as", "s éjour", "d 'emploi"):
        assert spacing_artifact not in table_text


def test_positioned_word_rebuild_requires_complete_matching_evidence():
    bbox = (0.0, 0.0, 1.0, 1.0)
    words = (
        LayoutWord("Alpha", (0.1, 0.1, 0.2, 0.2), 0, 0, 0),
        LayoutWord("Beta", (0.1, 0.3, 0.2, 0.4), 0, 2, 0),
    )

    assert _text_from_positioned_words(words, bbox, "AlphaBeta") == "Alpha\n\nBeta"
    assert _text_from_positioned_words(words, bbox, "different value") == "different value"
    assert _text_from_positioned_words((), bbox, "fallback") == "fallback"


def test_grid_geometry_distinguishes_horizontal_merge_from_an_empty_cell():
    rows = [
        SimpleNamespace(
            bbox=(0.0, 0.0, 300.0, 50.0),
            cells=[
                (0.0, 0.0, 200.0, 50.0),
                None,
                (200.0, 0.0, 300.0, 50.0),
            ],
        ),
        SimpleNamespace(
            bbox=(0.0, 50.0, 300.0, 100.0),
            cells=[
                (0.0, 50.0, 100.0, 100.0),
                (100.0, 50.0, 200.0, 100.0),
                (200.0, 50.0, 300.0, 100.0),
            ],
        ),
    ]

    semantics = _cell_grid_semantics(
        rows,
        ((0.0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1.0)),
        (0.0, 0.0, 300.0, 100.0),
        300.0,
        100.0,
    )

    assert semantics[(0, 0)] == ("value", 2, 1, None)
    assert semantics[(0, 1)] == ("covered", 1, 1, (0, 0))
    # This slot has its own geometry. An empty extracted value is therefore
    # an explicit empty cell, not a merged-cell continuation.
    assert semantics[(0, 2)] == ("value", 1, 1, None)


def test_grid_geometry_identifies_vertical_merged_cell_coverage():
    rows = [
        SimpleNamespace(
            bbox=(0.0, 0.0, 200.0, 50.0),
            cells=[
                (0.0, 0.0, 100.0, 100.0),
                (100.0, 0.0, 200.0, 50.0),
            ],
        ),
        SimpleNamespace(
            bbox=(0.0, 50.0, 200.0, 100.0),
            cells=[
                None,
                (100.0, 50.0, 200.0, 100.0),
            ],
        ),
    ]

    semantics = _cell_grid_semantics(
        rows,
        ((0.0, 0.5), (0.5, 1.0)),
        (0.0, 0.0, 200.0, 100.0),
        200.0,
        100.0,
    )

    assert semantics[(0, 0)] == ("value", 1, 2, None)
    assert semantics[(1, 0)] == ("covered", 1, 1, (0, 0))


def test_real_pymupdf_rows_do_not_inherit_rowspan_from_a_neighboring_anchor():
    pdf = pymupdf.open()
    page = pdf.new_page(width=300, height=240)
    x_positions = (30, 110, 190, 270)
    y_positions = (30, 80, 130, 180)
    for x_position in x_positions:
        page.draw_line((x_position, y_positions[0]), (x_position, y_positions[-1]))
    for y_position in (y_positions[0], y_positions[1], y_positions[-1]):
        page.draw_line((x_positions[0], y_position), (x_positions[-1], y_position))
    page.draw_line(
        (x_positions[1], y_positions[2]),
        (x_positions[-1], y_positions[2]),
    )
    for x_position, y_position, text in (
        (40, 60, "Category"),
        (120, 60, "City"),
        (200, 60, "Status"),
        (40, 110, "France"),
        (120, 110, "Paris"),
        (200, 110, "Active"),
        (120, 160, "Lyon"),
        (200, 160, "Inactive"),
    ):
        page.insert_text((x_position, y_position), text, fontsize=9)
    raw_bytes = pdf.tobytes()
    pdf.close()

    [evidence] = _collect_evidence(raw_bytes, (1,))
    table = evidence.tables[0]

    assert table.rows[1].cells[0].row_span == 2
    assert table.rows[2].cells[0].slot_state == "covered"
    assert table.rows[2].cells[0].covered_by == (1, 0)
    assert table.rows[2].cells[1].row_span == 1
    assert table.rows[2].cells[2].row_span == 1
