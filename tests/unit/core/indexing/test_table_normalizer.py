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
from core.models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from core.prompts.vlm_prompt_builder import wrap_caption
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

    # This is the production Marker shape observed for the fixture: a Markdown
    # anchor, a plain-text middle continuation, then a synthetic-column table.
    assert "|Col1|Col2|Col3|Col4|" not in parsed.raw_text_blocks[0].text
    assert "2.1. Si vous occupez toujours" in parsed.raw_text_blocks[1].text
    assert "|" not in parsed.raw_text_blocks[1].text
    assert parsed.raw_text_blocks[2].text.startswith("|Col1|Col2|Col3|Col4|")

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
    assert [cell.column_name for cell in first.cells][1:5] == [
        "Catégorie de titre de séjour",
        "Libellé",
        "Référence du CESEDA",
        "Pièces justificatives",
    ]
    assert parsed.raw_text_blocks == result.raw_text_blocks
    normalized_text = "\n".join(block.text for block in result.effective_text_blocks())
    assert normalized_text.count("2. Pièces à fournir lorsque") == 1
    assert "Row: row-" not in normalized_text

    for row in rows:
        for cell in row.cells:
            if cell.text:
                assert cell.source_fragments
                for fragment in cell.source_fragments:
                    raw = result.raw_text_blocks[fragment.source_block_index].text
                    assert raw[fragment.char_start : fragment.char_end]


@pytest.mark.asyncio
async def test_reconstructs_when_marker_caption_replaces_the_sparse_continuation():
    document = Document(
        filename=FIXTURE.name,
        content_type=DocumentType.PDF,
        raw_bytes=FIXTURE.read_bytes(),
    )
    parsed = await PyMuPDFParser().parse(document)
    caption = parsed.text_blocks[1].text
    markdown_ref = "![](_page_1_Picture_0.jpeg)"
    unrelated_ref = "![](_page_1_Picture_1.jpeg)"
    unrelated_caption = "A decorative seal that is unrelated to the legal table."
    raw_blocks = [block.model_copy(deep=True) for block in parsed.text_blocks]
    raw_blocks[1] = raw_blocks[1].model_copy(update={"text": f"{markdown_ref}\n\n{unrelated_ref}"})
    working_blocks = [block.model_copy(deep=True) for block in parsed.text_blocks]
    working_blocks[1] = working_blocks[1].model_copy(
        update={"text": f"{wrap_caption(caption)}\n\n{wrap_caption(unrelated_caption)}"}
    )
    marker_output = parsed.model_copy(
        update={
            "raw_text_blocks": raw_blocks,
            "text_blocks": working_blocks,
            "images": [
                ImageBlock(
                    page_number=2,
                    caption=caption,
                    metadata={
                        "markdown_ref": markdown_ref,
                        "marker_key": "_page_1_Picture_0.jpeg",
                    },
                ),
                ImageBlock(
                    page_number=2,
                    caption=unrelated_caption,
                    metadata={
                        "markdown_ref": unrelated_ref,
                        "marker_key": "_page_1_Picture_1.jpeg",
                    },
                ),
            ],
        }
    )

    result = await DeterministicTableNormalizer(PyMuPDFTableEvidenceProvider()).normalize(
        document,
        marker_output,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert result.normalization_report.status == "normalized"
    assert len(rows) == 2
    assert rows[0].page_start == 1
    assert rows[0].page_end == 3
    assert "L. 421-1" in "\n".join(cell.text for cell in rows[0].cells)
    assert "4.2. Si vous n'occupez plus" in "\n".join(cell.text for cell in rows[0].cells)
    assert "L. 421-3" in "\n".join(cell.text for cell in rows[1].cells)
    assert result.raw_text_blocks == raw_blocks
    normalized_text = "\n".join(block.text for block in result.effective_text_blocks())
    assert markdown_ref not in normalized_text
    assert caption not in normalized_text
    assert unrelated_caption in normalized_text
    assert normalized_text.count("4.2. Si vous n'occupez plus") == 1

    image_fragments = [
        fragment for cell in rows[0].cells for fragment in cell.source_fragments if fragment.source_kind == "pdf_layout"
    ]
    assert image_fragments
    assert all(fragment.evidence_provider == "pymupdf" for fragment in image_fragments)
    assert all(fragment.page_number == 2 for fragment in image_fragments)
    assert all(
        raw_blocks[fragment.source_block_index].text[fragment.char_start : fragment.char_end] == markdown_ref
        for fragment in image_fragments
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_marker_caption_alignment_fails_open_when_evidence_is_unsafe(ambiguous):
    document = Document(
        filename=FIXTURE.name,
        content_type=DocumentType.PDF,
        raw_bytes=FIXTURE.read_bytes(),
    )
    parsed = await PyMuPDFParser().parse(document)
    evidence_text = parsed.text_blocks[1].text
    captions = [evidence_text, evidence_text] if ambiguous else ["An unrelated photograph of a building."]
    refs = [f"![](_page_1_Picture_{index}.jpeg)" for index in range(len(captions))]
    raw_blocks = [block.model_copy(deep=True) for block in parsed.text_blocks]
    raw_blocks[1] = raw_blocks[1].model_copy(update={"text": "\n\n".join(refs)})
    working_blocks = [block.model_copy(deep=True) for block in parsed.text_blocks]
    working_blocks[1] = working_blocks[1].model_copy(
        update={"text": "\n\n".join(wrap_caption(caption) for caption in captions)}
    )
    marker_output = parsed.model_copy(
        update={
            "raw_text_blocks": raw_blocks,
            "text_blocks": working_blocks,
            "images": [
                ImageBlock(
                    page_number=2,
                    caption=caption,
                    metadata={
                        "markdown_ref": ref,
                        "marker_key": f"_page_1_Picture_{index}.jpeg",
                    },
                )
                for index, (ref, caption) in enumerate(zip(refs, captions, strict=True))
            ],
        }
    )

    result = await DeterministicTableNormalizer(PyMuPDFTableEvidenceProvider()).normalize(
        document,
        marker_output,
        TableReconstructionConfig(mode="automatic"),
    )

    assert result.normalized_text_blocks is None
    assert result.normalization_report.status == "unchanged"
    assert result.raw_text_blocks == raw_blocks
    assert result.effective_text_blocks() == working_blocks


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
async def test_short_decorative_caption_does_not_trigger_layout_scanning():
    markdown_ref = "![](_page_0_Picture_0.jpeg)"
    raw = TextBlock(text=markdown_ref, page_number=1)
    processed = ProcessedDocument(
        text_blocks=[raw.model_copy(update={"text": wrap_caption("Company logo")})],
        raw_text_blocks=[raw],
        images=[
            ImageBlock(
                page_number=1,
                caption="Company logo",
                metadata={"markdown_ref": markdown_ref},
            )
        ],
        page_count=1,
    )
    provider = FakeEvidenceProvider({})

    result = await DeterministicTableNormalizer(provider).normalize(
        Document(filename="decorative.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf"),
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    assert provider.calls == []
    assert result.normalized_text_blocks is None
    assert result.effective_text_blocks() == processed.text_blocks


@pytest.mark.asyncio
async def test_untrusted_parser_header_cannot_rename_reconstructed_columns():
    header = LayoutRowEvidence(
        cells=(
            LayoutCellEvidence(0, "Reference", (0.0, 0.70, 0.30, 0.72)),
            LayoutCellEvidence(1, "Documents", (0.30, 0.70, 1.0, 0.72)),
        ),
        bbox=(0.0, 0.70, 1.0, 0.72),
    )
    data_rows = tuple(
        LayoutRowEvidence(
            cells=(
                LayoutCellEvidence(0, f"A{index}", (0.0, 0.72, 0.30, 0.95)),
                LayoutCellEvidence(1, f"description-{index}", (0.30, 0.72, 1.0, 0.95)),
            ),
            bbox=(0.0, 0.72, 1.0, 0.95),
        )
        for index in range(15)
    )
    table = LayoutTableEvidence(
        page_number=1,
        bbox=(0.0, 0.70, 1.0, 0.95),
        column_bounds=((0.0, 0.30), (0.30, 1.0)),
        rows=(header, *data_rows),
    )
    pages = {
        1: PageLayoutEvidence(page_number=1, width=100, height=100, tables=(table,)),
        2: PageLayoutEvidence(
            page_number=2,
            width=100,
            height=100,
            words=(LayoutWord("continued", (0.40, 0.05, 0.65, 0.10), 0, 0, 0),),
        ),
    }
    markdown_rows = "\n".join(f"| A{index} | description-{index} |" for index in range(15))
    raw_blocks = [
        TextBlock(
            text=f"| Reference wrong | Documents wrong |\n|---|---|\n{markdown_rows}",
            page_number=1,
        ),
        TextBlock(text="continued", page_number=2),
    ]
    processed = ProcessedDocument(
        text_blocks=[block.model_copy(deep=True) for block in raw_blocks],
        raw_text_blocks=raw_blocks,
        page_count=2,
    )
    document = Document(filename="synthetic.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf")

    result = await DeterministicTableNormalizer(FakeEvidenceProvider(pages)).normalize(
        document,
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert rows
    assert [cell.column_name for cell in rows[0].cells] == ["Reference", "Documents"]


@pytest.mark.asyncio
@pytest.mark.parametrize("whole_table_image", [False, True])
async def test_reconstructs_when_marker_caption_represents_table_content(whole_table_image):
    caption = " ".join(f"supporting-document-{index}" for index in range(20))
    markdown_ref = "![](_page_0_Picture_0.jpeg)"
    header = LayoutRowEvidence(
        cells=(
            LayoutCellEvidence(0, "Type C visa", (0.0, 0.80, 0.30, 0.85)),
            LayoutCellEvidence(1, "Description", (0.30, 0.80, 1.0, 0.85)),
        ),
        bbox=(0.0, 0.80, 1.0, 0.85),
    )
    data = LayoutRowEvidence(
        cells=(
            LayoutCellEvidence(0, "A", (0.0, 0.85, 0.30, 0.96)),
            LayoutCellEvidence(1, caption, (0.30, 0.85, 1.0, 0.96)),
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
            words=(LayoutWord("continued", (0.40, 0.05, 0.65, 0.10), 0, 0, 0),),
        ),
    }
    anchor_text = (
        markdown_ref if whole_table_image else f"| Type C visa | Description |\n|---|---|\n| A | {markdown_ref} |"
    )
    raw_blocks = [TextBlock(text=anchor_text, page_number=1), TextBlock(text="continued", page_number=2)]
    working_blocks = [
        raw_blocks[0].model_copy(update={"text": raw_blocks[0].text.replace(markdown_ref, wrap_caption(caption))}),
        raw_blocks[1].model_copy(deep=True),
    ]
    processed = ProcessedDocument(
        text_blocks=working_blocks,
        raw_text_blocks=raw_blocks,
        images=[
            ImageBlock(
                page_number=1,
                caption=caption,
                metadata={"markdown_ref": markdown_ref, "marker_key": "_page_0_Picture_0.jpeg"},
            )
        ],
        page_count=2,
    )
    document = Document(filename="synthetic.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf")

    result = await DeterministicTableNormalizer(FakeEvidenceProvider(pages)).normalize(
        document,
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert len(rows) == 1
    assert rows[0].page_end == 2
    assert rows[0].cells[0].column_name == "Type C visa"
    assert caption in rows[0].cells[1].text
    assert "continued" in rows[0].cells[1].text
    assert any(fragment.source_kind == "pdf_layout" for fragment in rows[0].cells[1].source_fragments)
    normalized = "\n".join(block.text for block in result.effective_text_blocks())
    assert normalized.count(caption) == 1
    assert markdown_ref not in normalized
    assert "<image_description>" not in normalized


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
