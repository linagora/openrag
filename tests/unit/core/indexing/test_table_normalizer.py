from pathlib import Path

import pytest
from core.config.table_reconstruction import TableReconstructionConfig
from core.indexing.parsers.pdf.pymupdf import PyMuPDFParser
from core.indexing.structure_normalizer import (
    LayoutCellEvidence,
    LayoutRowEvidence,
    LayoutTableEvidence,
    LayoutWord,
    PageLayoutEvidence,
    TableLayoutEvidenceProvider,
)
from core.indexing.table_normalizer import DeterministicTableNormalizer
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from services.workers.layout import PyMuPDFTableEvidenceProvider

FIXTURE = Path(__file__).parents[3] / "resources" / "cross_page_table_rows_803_805.pdf"


async def _normalize(config: TableReconstructionConfig | None = None, *, corrupt_identity: bool = False):
    document = Document(
        filename=FIXTURE.name,
        content_type=DocumentType.PDF,
        raw_bytes=FIXTURE.read_bytes(),
    )
    parsed = await PyMuPDFParser().parse(document)
    raw_blocks = [block.model_copy(deep=True) for block in parsed.text_blocks]
    if corrupt_identity:
        raw_blocks[0].text = raw_blocks[0].text.replace('CST portant la mention " salarié "', "unrelated identity")
    parsed = parsed.model_copy(update={"raw_text_blocks": raw_blocks})
    result = await DeterministicTableNormalizer(PyMuPDFTableEvidenceProvider()).normalize(
        document,
        parsed,
        config or TableReconstructionConfig(mode="automatic"),
    )
    return parsed, result


@pytest.mark.asyncio
async def test_reconstructs_primary_regression_and_keeps_the_next_row_separate():
    parsed, result = await _normalize()

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert result.normalization_report.status == "normalized"
    assert result.normalization_report.reconstructed_row_count == 2
    assert len(rows) == 2

    first, second = rows
    first_text = "\n".join(cell.text for cell in first.cells)
    second_text = "\n".join(cell.text for cell in second.cells)
    assert first.page_start == 1
    assert first.page_end == 3
    assert "CST portant la mention" in first_text
    assert "L. 421-1" in first_text
    assert "4.2. Si vous n'occupez plus" in first_text
    assert "travailleur temporaire" not in first_text
    assert "travailleur temporaire" in second_text
    assert "L. 421-3" in second_text
    assert parsed.raw_text_blocks == result.raw_text_blocks
    normalized_text = "\n".join(block.text for block in result.effective_text_blocks())
    assert normalized_text.count("2. Pièces à fournir lorsque") == 1

    for row in rows:
        for cell in row.cells:
            if cell.text:
                assert cell.source_fragments
                for fragment in cell.source_fragments:
                    raw = result.raw_text_blocks[fragment.source_block_index].text
                    assert raw[fragment.char_start : fragment.char_end]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        TableReconstructionConfig(mode="automatic", same_table_min_confidence=0.98),
        TableReconstructionConfig(mode="automatic", row_continuation_min_confidence=0.99),
    ],
)
async def test_independent_boundary_thresholds_can_reject_the_merge(config):
    _, result = await _normalize(config)

    assert result.normalized_text_blocks is None
    assert result.normalization_report.status == "unchanged"


@pytest.mark.asyncio
async def test_uncertain_cell_alignment_preserves_the_parser_output():
    _, result = await _normalize(corrupt_identity=True)

    assert result.normalized_text_blocks is None
    assert result.normalization_report.status == "unchanged"


class FakeEvidenceProvider(TableLayoutEvidenceProvider):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def collect(self, document, page_numbers):
        self.calls.append(set(page_numbers))
        return [self.pages[page] for page in sorted(page_numbers) if page in self.pages]


@pytest.mark.asyncio
async def test_candidate_window_expands_across_two_sparse_continuation_pages():
    header = LayoutRowEvidence(
        cells=(
            LayoutCellEvidence(0, "ID", (0.0, 0.80, 0.30, 0.85)),
            LayoutCellEvidence(1, "Description", (0.30, 0.80, 1.0, 0.85)),
        ),
        bbox=(0.0, 0.80, 1.0, 0.85),
    )
    data = LayoutRowEvidence(
        cells=(
            LayoutCellEvidence(0, "A", (0.0, 0.85, 0.30, 0.96)),
            LayoutCellEvidence(1, "beginning", (0.30, 0.85, 1.0, 0.96)),
        ),
        bbox=(0.0, 0.85, 1.0, 0.96),
    )
    pages = {
        1: PageLayoutEvidence(
            page_number=1,
            width=100,
            height=100,
            tables=(
                LayoutTableEvidence(
                    page_number=1,
                    bbox=(0.0, 0.80, 1.0, 0.96),
                    column_bounds=((0.0, 0.30), (0.30, 1.0)),
                    rows=(header, data),
                ),
            ),
        ),
        2: PageLayoutEvidence(
            page_number=2,
            width=100,
            height=100,
            words=(
                LayoutWord("middle", (0.40, 0.05, 0.60, 0.10), 0, 0, 0),
                LayoutWord("continues", (0.40, 0.84, 0.70, 0.88), 1, 0, 0),
            ),
        ),
        3: PageLayoutEvidence(
            page_number=3,
            width=100,
            height=100,
            words=(LayoutWord("end", (0.40, 0.05, 0.55, 0.10), 0, 0, 0),),
        ),
    }
    provider = FakeEvidenceProvider(pages)
    document = Document(filename="synthetic.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf")
    raw_blocks = [
        TextBlock(text="| ID | Description |\n|---|---|\n| A | beginning |", page_number=1),
        TextBlock(text="middle\n\ncontinues", page_number=2),
        TextBlock(text="end", page_number=3),
    ]
    processed = ProcessedDocument(
        text_blocks=[block.model_copy(deep=True) for block in raw_blocks],
        raw_text_blocks=raw_blocks,
        page_count=3,
    )

    result = await DeterministicTableNormalizer(provider).normalize(
        document,
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert provider.calls[0] == {1, 2}
    assert provider.calls[-1] == {3}
    assert len(rows) == 1
    assert rows[0].page_end == 3
    assert "middle" in rows[0].cells[1].text
    assert "end" in rows[0].cells[1].text
