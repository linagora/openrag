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
    legends = [block.table_legend for block in result.effective_text_blocks() if block.table_legend is not None]
    assert result.normalization_report.status == "normalized"
    assert result.normalization_report.reconstructed_row_count == 2
    assert len(rows) == 2
    assert len(legends) == 1

    first, second = rows
    first_text = "\n".join(cell.text for cell in first.cells)
    second_text = "\n".join(cell.text for cell in second.cells)
    assert first.page_start == 1
    assert first.page_end == 3
    assert first.table_title == "ANNEXE"
    assert first.section_path == ["Article Annexe 10"]
    assert len(first.scope_fragments) == 2
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
    assert [(entry.abbreviation, entry.meaning) for entry in legends[0].entries] == [
        ("APS", "autorisation provisoire de séjour"),
        ("CST", "carte de séjour temporaire"),
        ("CSP", "carte de séjour pluriannuelle"),
        ("CR", "carte de résident"),
    ]
    assert parsed.raw_text_blocks == result.raw_text_blocks
    normalized_text = "\n".join(block.text for block in result.effective_text_blocks())
    assert normalized_text.count("2. Pièces à fournir lorsque") == 1
    assert "Row: row-" not in normalized_text
    assert "LibelléAPS" not in normalized_text
    assert "CST means “carte de séjour temporaire”" in normalized_text
    row_text = next(block.text for block in result.effective_text_blocks() if block.table_row is first)
    assert "APS means" not in row_text
    assert "the value “L. 421-1” in column “Référence du CESEDA”" in row_text
    assert '"salarié"' in row_text
    assert '" salarié "' not in row_text
    assert "le 5 juin" in row_text
    for spacing_artifact in ("c as", "s éjour", "d 'emploi"):
        assert spacing_artifact not in row_text

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
        fragment
        for cell in rows[0].cells
        for fragment in cell.source_fragments
        if fragment.source_kind == "pdf_layout" and fragment.source_ref is not None
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
    def __init__(self, pages, *, discovered=None):
        self.pages = pages
        self.discovered = set(discovered or ())
        self.calls = []

    async def discover(self, document):
        return set(self.discovered)

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

    assert result.normalized_text_blocks is None
    assert result.effective_text_blocks() == processed.text_blocks
    assert "Reference wrong" in result.effective_text_blocks()[0].text


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


_THREE_COLUMN_BOUNDS = ((0.10, 0.36), (0.36, 0.63), (0.63, 0.90))


def _three_column_row(
    values: tuple[str, str, str],
    y0: float,
    y1: float,
    *,
    cell_options: dict[int, dict] | None = None,
) -> LayoutRowEvidence:
    options = cell_options or {}
    cells = tuple(
        LayoutCellEvidence(
            column_index=column_index,
            text=value,
            bbox=None if options.get(column_index, {}).get("slot_state") == "covered" else (left, y0, right, y1),
            **options.get(column_index, {}),
        )
        for column_index, (value, (left, right)) in enumerate(zip(values, _THREE_COLUMN_BOUNDS, strict=True))
    )
    return LayoutRowEvidence(cells=cells, bbox=(0.10, y0, 0.90, y1))


def _one_page_table(
    rows: tuple[LayoutRowEvidence, ...],
    *,
    bbox: tuple[float, float, float, float] = (0.10, 0.25, 0.90, 0.65),
) -> PageLayoutEvidence:
    return PageLayoutEvidence(
        page_number=1,
        width=100,
        height=100,
        tables=(
            LayoutTableEvidence(
                page_number=1,
                bbox=bbox,
                column_bounds=_THREE_COLUMN_BOUNDS,
                rows=rows,
            ),
        ),
    )


async def _normalize_one_page_table(
    markdown: str,
    evidence: PageLayoutEvidence,
    *,
    filename: str,
) -> tuple[ProcessedDocument, ProcessedDocument]:
    raw = TextBlock(text=markdown, page_number=1)
    processed = ProcessedDocument(
        text_blocks=[raw.model_copy(deep=True)],
        raw_text_blocks=[raw],
        page_count=1,
    )
    result = await DeterministicTableNormalizer(FakeEvidenceProvider({1: evidence})).normalize(
        Document(
            filename=filename,
            content_type=DocumentType.PDF,
            raw_bytes=b"pdf",
        ),
        processed,
        TableReconstructionConfig(mode="automatic"),
    )
    return processed, result


@pytest.mark.asyncio
async def test_normalizes_an_ordinary_table_and_preserves_surrounding_paragraphs():
    evidence = _one_page_table(
        (
            _three_column_row(("aa", "bb", "cc"), 0.25, 0.35),
            _three_column_row(("22", "Paris", "Active"), 0.35, 0.45),
            _three_column_row(("35", "Lyon", "Inactive"), 0.45, 0.55),
        )
    )
    markdown = (
        "Paragraph before the table.\n\n"
        "# Table A\n\n"
        "| aa | bb | cc |\n"
        "|---|---|---|\n"
        "| 22 | Paris | Active |\n"
        "| 35 | Lyon | Inactive |\n\n"
        "Paragraph after the table."
    )

    parsed, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="table-a.pdf",
    )

    blocks = result.effective_text_blocks()
    rows = [block.table_row for block in blocks if block.table_row is not None]
    assert result.normalization_report.status == "normalized"
    assert result.normalization_report.reconstructed_row_count == 2
    assert len(rows) == 2
    assert [row.row_index for row in rows] == [1, 2]
    assert all(row.table_title == "Table A" for row in rows)
    assert all(row.section_path == [] for row in rows)
    assert [[cell.column_name for cell in row.cells] for row in rows] == [
        ["aa", "bb", "cc"],
        ["aa", "bb", "cc"],
    ]
    assert [[cell.text for cell in row.cells] for row in rows] == [
        ["22", "Paris", "Active"],
        ["35", "Lyon", "Inactive"],
    ]
    ordinary_text = "\n".join(block.text for block in blocks if block.table_row is None and block.table_legend is None)
    assert "Paragraph before the table." in ordinary_text
    assert "Paragraph after the table." in ordinary_text
    assert parsed.raw_text_blocks == result.raw_text_blocks


@pytest.mark.asyncio
async def test_untitled_table_does_not_invent_the_filename_as_its_title():
    evidence = _one_page_table(
        (
            _three_column_row(("aa", "bb", "cc"), 0.25, 0.35),
            _three_column_row(("22", "Paris", "Active"), 0.35, 0.45),
            _three_column_row(("35", "Lyon", "Inactive"), 0.45, 0.55),
        )
    )
    markdown = (
        "Paragraph before.\n\n"
        "| aa | bb | cc |\n"
        "|---|---|---|\n"
        "| 22 | Paris | Active |\n"
        "| 35 | Lyon | Inactive |\n\n"
        "Paragraph after."
    )

    _, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="must-not-become-the-table-title.pdf",
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert len(rows) == 2
    assert all(row.table_title is None for row in rows)
    assert all(row.section_path == [] for row in rows)
    assert "must-not-become-the-table-title.pdf" not in "\n".join(
        block.text for block in result.effective_text_blocks()
    )


@pytest.mark.asyncio
async def test_untitled_table_uses_heading_ancestry_without_inventing_a_title():
    evidence = _one_page_table(
        (
            _three_column_row(("aa", "bb", "cc"), 0.25, 0.35),
            _three_column_row(("22", "Paris", "Active"), 0.35, 0.45),
        )
    )
    markdown = (
        "# Report\n\n"
        "## Previous section\n\n"
        "Previous prose.\n\n"
        "## Results\n\n"
        "| aa | bb | cc |\n"
        "|---|---|---|\n"
        "| 22 | Paris | Active |"
    )

    _, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="report.pdf",
    )

    [row] = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert row.table_title is None
    assert row.section_path == ["Report", "Results"]
    assert "Previous section" not in row.section_path
    assert len(row.scope_fragments) == 2


@pytest.mark.asyncio
async def test_vertical_merge_is_inherited_while_explicit_empty_stays_empty():
    evidence = _one_page_table(
        (
            _three_column_row(("Category", "City", "Status"), 0.25, 0.35),
            _three_column_row(
                ("France", "Paris", "Active"),
                0.35,
                0.45,
                cell_options={0: {"row_span": 2}},
            ),
            _three_column_row(
                ("", "", "Inactive"),
                0.45,
                0.55,
                cell_options={
                    0: {
                        "slot_state": "covered",
                        "covered_by": (1, 0),
                    },
                    1: {"slot_state": "explicit_empty"},
                },
            ),
        )
    )
    markdown = "| Category | City | Status |\n|---|---|---|\n| France | Paris | Active |\n| | | Inactive |"

    _, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="merged.pdf",
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert len(rows) == 2
    first, second = rows
    assert first.cells[0].text == "France"
    assert first.cells[0].row_span == 2
    assert second.cells[0].text == "France"
    assert second.cells[0].inherited is True
    assert second.cells[0].inherited_from == (1, 0)
    assert second.cells[0].explicit_empty is False
    assert second.cells[1].text == ""
    assert second.cells[1].explicit_empty is True
    assert second.cells[1].inherited is False
    assert second.cells[2].text == "Inactive"


@pytest.mark.asyncio
async def test_unknown_merged_slot_fails_open_without_consuming_the_table():
    evidence = _one_page_table(
        (
            _three_column_row(("Category", "City", "Status"), 0.25, 0.35),
            _three_column_row(("France", "Paris", "Active"), 0.35, 0.45),
            _three_column_row(
                ("", "Lyon", "Inactive"),
                0.45,
                0.55,
                cell_options={0: {"slot_state": "unknown"}},
            ),
        )
    )
    markdown = (
        "Usable paragraph before the table.\n\n"
        "| Category | City | Status |\n"
        "|---|---|---|\n"
        "| France | Paris | Active |\n"
        "| | Lyon | Inactive |\n\n"
        "Usable paragraph after the table."
    )

    parsed, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="ambiguous.pdf",
    )

    assert result.normalized_text_blocks is None
    assert result.normalization_report.status == "unchanged"
    assert result.effective_text_blocks() == parsed.text_blocks
    assert result.raw_text_blocks == parsed.raw_text_blocks


@pytest.mark.asyncio
async def test_rows_after_a_continuation_receive_distinct_logical_numbers():
    page_one_rows = (
        _three_column_row(("ID", "Type", "Description"), 0.75, 0.82),
        _three_column_row(("A", "Alpha", "Beginning"), 0.82, 0.96),
    )
    page_two_rows = (
        _three_column_row(("", "", "Continuation"), 0.03, 0.12),
        _three_column_row(("B", "Beta", "Second row"), 0.12, 0.24),
        _three_column_row(("C", "Gamma", "Third row"), 0.24, 0.36),
    )
    pages = {
        1: _one_page_table(page_one_rows, bbox=(0.10, 0.75, 0.90, 0.96)),
        2: PageLayoutEvidence(
            page_number=2,
            width=100,
            height=100,
            tables=(
                LayoutTableEvidence(
                    page_number=2,
                    bbox=(0.10, 0.03, 0.90, 0.36),
                    column_bounds=_THREE_COLUMN_BOUNDS,
                    rows=page_two_rows,
                ),
            ),
        ),
    }
    raw_blocks = [
        TextBlock(
            text="| ID | Type | Description |\n|---|---|---|\n| A | Alpha | Beginning |",
            page_number=1,
        ),
        TextBlock(
            text=(
                "| Col1 | Col2 | Continuation |\n|---|---|---|\n| B | Beta | Second row |\n| C | Gamma | Third row |"
            ),
            page_number=2,
        ),
    ]
    processed = ProcessedDocument(
        text_blocks=[block.model_copy(deep=True) for block in raw_blocks],
        raw_text_blocks=raw_blocks,
        page_count=2,
    )

    result = await DeterministicTableNormalizer(FakeEvidenceProvider(pages)).normalize(
        Document(filename="continued.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf"),
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert [row.row_index for row in rows] == [1, 2, 3]
    assert len({row.row_id for row in rows}) == 3
    assert "Continuation" in rows[0].cells[2].text
    assert [row.cells[0].text for row in rows[1:]] == ["B", "C"]


@pytest.mark.asyncio
async def test_distinct_table_on_the_next_page_does_not_discard_the_first_table():
    page_one_rows = (
        _three_column_row(("ID", "Type", "Description"), 0.75, 0.82),
        _three_column_row(("A", "Alpha", "First table"), 0.82, 0.96),
    )
    page_two_rows = (
        _three_column_row(("ID", "Type", "Description"), 0.03, 0.10),
        _three_column_row(("B", "Beta", "Second table"), 0.10, 0.24),
    )
    pages = {
        1: _one_page_table(page_one_rows, bbox=(0.10, 0.75, 0.90, 0.96)),
        2: PageLayoutEvidence(
            page_number=2,
            width=100,
            height=100,
            tables=(
                LayoutTableEvidence(
                    page_number=2,
                    bbox=(0.10, 0.03, 0.90, 0.24),
                    column_bounds=_THREE_COLUMN_BOUNDS,
                    rows=page_two_rows,
                ),
            ),
        ),
    }
    raw_blocks = [
        TextBlock(
            text="| ID | Type | Description |\n|---|---|---|\n| A | Alpha | First table |",
            page_number=1,
        ),
        TextBlock(
            text="| ID | Type | Description |\n|---|---|---|\n| B | Beta | Second table |",
            page_number=2,
        ),
    ]
    processed = ProcessedDocument(
        text_blocks=[block.model_copy(deep=True) for block in raw_blocks],
        raw_text_blocks=raw_blocks,
        page_count=2,
    )

    result = await DeterministicTableNormalizer(FakeEvidenceProvider(pages)).normalize(
        Document(filename="two-tables.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf"),
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert result.normalization_report.status == "partial_fallback"
    assert [row.cells[0].text for row in rows] == ["A", "B"]
    assert len({row.table_id for row in rows}) == 2


@pytest.mark.asyncio
async def test_unmatched_parser_row_fails_open_without_losing_its_content():
    evidence = _one_page_table(
        (
            _three_column_row(("Name", "City", "Status"), 0.25, 0.35),
            _three_column_row(("one", "Paris", "Active"), 0.35, 0.45),
            # PyMuPDF missed the middle row but still found the row below it.
            _three_column_row(("three", "Lyon", "Inactive"), 0.55, 0.65),
        )
    )
    markdown = (
        "| Name | City | Status |\n"
        "|---|---|---|\n"
        "| one | Paris | Active |\n"
        "| two | Marseille | Pending |\n"
        "| three | Lyon | Inactive |"
    )

    parsed, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="parser-row-mismatch.pdf",
    )

    assert result.normalized_text_blocks is None
    assert result.normalization_report.status == "unchanged"
    assert result.effective_text_blocks() == parsed.text_blocks
    assert "two | Marseille | Pending" in result.effective_text_blocks()[0].text


@pytest.mark.asyncio
async def test_parser_value_in_layout_empty_cell_fails_open():
    evidence = _one_page_table(
        (
            _three_column_row(("Name", "City", "Status"), 0.15, 0.25),
            _three_column_row(("one", "Paris", "Active"), 0.25, 0.35),
            _three_column_row(("two", "Lyon", "Inactive"), 0.35, 0.45),
            _three_column_row(("three", "Nice", "Pending"), 0.45, 0.55),
            _three_column_row(
                ("", "Toulouse", "Active"),
                0.55,
                0.65,
                cell_options={0: {"slot_state": "explicit_empty"}},
            ),
        ),
        bbox=(0.10, 0.15, 0.90, 0.65),
    )
    markdown = (
        "| Name | City | Status |\n"
        "|---|---|---|\n"
        "| one | Paris | Active |\n"
        "| two | Lyon | Inactive |\n"
        "| three | Nice | Pending |\n"
        "| SECRET | Toulouse | Active |"
    )

    parsed, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="empty-cell-disagreement.pdf",
    )

    assert result.normalized_text_blocks is None
    assert result.normalization_report.status == "unchanged"
    assert result.effective_text_blocks() == parsed.text_blocks
    assert "SECRET" in result.effective_text_blocks()[0].text


@pytest.mark.asyncio
async def test_same_page_tables_with_the_same_headers_have_distinct_table_ids():
    first_table = LayoutTableEvidence(
        page_number=1,
        bbox=(0.10, 0.10, 0.90, 0.35),
        column_bounds=_THREE_COLUMN_BOUNDS,
        rows=(
            _three_column_row(("aa", "bb", "cc"), 0.10, 0.20),
            _three_column_row(("22", "Paris", "Active"), 0.20, 0.30),
        ),
    )
    second_table = LayoutTableEvidence(
        page_number=1,
        bbox=(0.10, 0.50, 0.90, 0.75),
        column_bounds=_THREE_COLUMN_BOUNDS,
        rows=(
            _three_column_row(("aa", "bb", "cc"), 0.50, 0.60),
            _three_column_row(("35", "Lyon", "Inactive"), 0.60, 0.70),
        ),
    )
    evidence = PageLayoutEvidence(
        page_number=1,
        width=100,
        height=100,
        tables=(first_table, second_table),
    )
    markdown = (
        "# Table A\n\n"
        "| aa | bb | cc |\n"
        "|---|---|---|\n"
        "| 22 | Paris | Active |\n\n"
        "# Table B\n\n"
        "| aa | bb | cc |\n"
        "|---|---|---|\n"
        "| 35 | Lyon | Inactive |"
    )

    _, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="same-headers.pdf",
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert len(rows) == 2
    assert len({row.table_id for row in rows}) == 2


@pytest.mark.asyncio
async def test_layout_value_that_omits_parser_suffix_fails_open():
    evidence = _one_page_table(
        (
            _three_column_row(("Name", "City", "Description"), 0.25, 0.35),
            _three_column_row(
                ("one", "Paris", "A sufficiently long description without its suffix"),
                0.35,
                0.45,
            ),
        )
    )
    markdown = (
        "| Name | City | Description |\n"
        "|---|---|---|\n"
        "| one | Paris | A sufficiently long description without its suffix SECRET-42 |"
    )

    parsed, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="parser-suffix.pdf",
    )

    assert result.normalized_text_blocks is None
    assert result.effective_text_blocks() == parsed.text_blocks
    assert "SECRET-42" in result.effective_text_blocks()[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("page_prefix", ["# New section", "A separate table follows"])
async def test_unaccounted_page_prefix_prevents_a_compatible_cross_page_merge(
    page_prefix,
):
    page_one = _one_page_table(
        (
            _three_column_row(("ID", "Type", "Description"), 0.75, 0.82),
            _three_column_row(("A", "Alpha", "Beginning"), 0.82, 0.96),
        ),
        bbox=(0.10, 0.75, 0.90, 0.96),
    )
    page_two_table = LayoutTableEvidence(
        page_number=2,
        bbox=(0.10, 0.03, 0.90, 0.20),
        column_bounds=_THREE_COLUMN_BOUNDS,
        rows=(
            _three_column_row(("", "", "Unrelated text"), 0.03, 0.10),
            _three_column_row(("B", "Beta", "New section row"), 0.10, 0.20),
        ),
    )
    pages = {
        1: page_one,
        2: PageLayoutEvidence(
            page_number=2,
            width=100,
            height=100,
            tables=(page_two_table,),
        ),
    }
    raw_blocks = [
        TextBlock(
            text="| ID | Type | Description |\n|---|---|---|\n| A | Alpha | Beginning |",
            page_number=1,
        ),
        TextBlock(
            text=(
                f"{page_prefix}\n\n"
                "| Col1 | Col2 | Description |\n"
                "|---|---|---|\n"
                "| | | Unrelated text |\n"
                "| B | Beta | New section row |"
            ),
            page_number=2,
        ),
    ]
    processed = ProcessedDocument(
        text_blocks=[block.model_copy(deep=True) for block in raw_blocks],
        raw_text_blocks=raw_blocks,
        page_count=2,
    )

    result = await DeterministicTableNormalizer(FakeEvidenceProvider(pages)).normalize(
        Document(filename="sections.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf"),
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    first = next(row for row in rows if row.cells[0].text == "A")
    assert first.page_end == 1
    assert "Unrelated text" not in first.cells[2].text


@pytest.mark.asyncio
async def test_repeated_page_header_is_not_emitted_as_data_before_continuation():
    pages = {
        1: _one_page_table(
            (
                _three_column_row(("ID", "Type", "Description"), 0.75, 0.82),
                _three_column_row(("A", "Alpha", "Beginning"), 0.82, 0.96),
            ),
            bbox=(0.10, 0.75, 0.90, 0.96),
        ),
        2: PageLayoutEvidence(
            page_number=2,
            width=100,
            height=100,
            tables=(
                LayoutTableEvidence(
                    page_number=2,
                    bbox=(0.10, 0.03, 0.90, 0.30),
                    column_bounds=_THREE_COLUMN_BOUNDS,
                    rows=(
                        _three_column_row(("ID", "Type", "Description"), 0.03, 0.09),
                        _three_column_row(("", "", "Continuation"), 0.09, 0.16),
                        _three_column_row(("B", "Beta", "Second row"), 0.16, 0.30),
                    ),
                ),
            ),
        ),
    }
    raw_blocks = [
        TextBlock(
            text="| ID | Type | Description |\n|---|---|---|\n| A | Alpha | Beginning |",
            page_number=1,
        ),
        TextBlock(
            text=("| ID | Type | Description |\n|---|---|---|\n| | | Continuation |\n| B | Beta | Second row |"),
            page_number=2,
        ),
    ]
    processed = ProcessedDocument(
        text_blocks=[block.model_copy(deep=True) for block in raw_blocks],
        raw_text_blocks=raw_blocks,
        page_count=2,
    )

    result = await DeterministicTableNormalizer(FakeEvidenceProvider(pages)).normalize(
        Document(filename="repeated-header.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf"),
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert [row.cells[0].text for row in rows] == ["A", "B"]
    assert "Continuation" in rows[0].cells[2].text
    assert all(row.cells[0].text != "ID" for row in rows)


@pytest.mark.asyncio
async def test_headerless_table_preserves_its_first_data_row():
    evidence = _one_page_table(
        (
            _three_column_row(("22", "Paris", "Active"), 0.25, 0.35),
            _three_column_row(("35", "Lyon", "Inactive"), 0.35, 0.45),
        )
    )
    markdown = "| 22 | Paris | Active |\n|---|---|---|\n| 35 | Lyon | Inactive |"

    _, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="headerless.pdf",
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert [[cell.text for cell in row.cells] for row in rows] == [
        ["22", "Paris", "Active"],
        ["35", "Lyon", "Inactive"],
    ]
    assert [cell.column_name for cell in rows[0].cells] == [
        "Column 1",
        "Column 2",
        "Column 3",
    ]


@pytest.mark.asyncio
async def test_explicit_all_text_markdown_header_supplies_column_names():
    evidence = _one_page_table(
        (
            _three_column_row(("Field", "Place", "Condition"), 0.25, 0.35),
            _three_column_row(("Alice", "Paris", "Active"), 0.35, 0.45),
        )
    )
    markdown = "| Field | Place | Condition |\n|---|---|---|\n| Alice | Paris | Active |"

    _, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="text-header.pdf",
    )

    [row] = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert [cell.column_name for cell in row.cells] == [
        "Field",
        "Place",
        "Condition",
    ]
    assert [cell.text for cell in row.cells] == ["Alice", "Paris", "Active"]


@pytest.mark.asyncio
async def test_later_untitled_table_does_not_inherit_an_earlier_title():
    evidence = PageLayoutEvidence(
        page_number=1,
        width=100,
        height=100,
        tables=(
            LayoutTableEvidence(
                page_number=1,
                bbox=(0.10, 0.10, 0.90, 0.35),
                column_bounds=_THREE_COLUMN_BOUNDS,
                rows=(
                    _three_column_row(("aa", "bb", "cc"), 0.10, 0.20),
                    _three_column_row(("22", "Paris", "Active"), 0.20, 0.30),
                ),
            ),
            LayoutTableEvidence(
                page_number=1,
                bbox=(0.10, 0.55, 0.90, 0.80),
                column_bounds=_THREE_COLUMN_BOUNDS,
                rows=(
                    _three_column_row(("aa", "bb", "cc"), 0.55, 0.65),
                    _three_column_row(("35", "Lyon", "Inactive"), 0.65, 0.75),
                ),
            ),
        ),
    )
    markdown = (
        "# Table A\n\n"
        "| aa | bb | cc |\n"
        "|---|---|---|\n"
        "| 22 | Paris | Active |\n\n"
        "The next results are separate.\n\n"
        "aa bb cc 35 Lyon Inactive"
    )

    _, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="local-caption.pdf",
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert [row.table_title for row in rows] == ["Table A", None]


@pytest.mark.asyncio
async def test_plain_text_tables_claim_a_caption_only_once():
    evidence = PageLayoutEvidence(
        page_number=1,
        width=100,
        height=100,
        tables=(
            LayoutTableEvidence(
                page_number=1,
                bbox=(0.10, 0.10, 0.90, 0.35),
                column_bounds=_THREE_COLUMN_BOUNDS,
                rows=(
                    _three_column_row(("aa", "bb", "cc"), 0.10, 0.20),
                    _three_column_row(("22", "Paris", "Active"), 0.20, 0.30),
                ),
            ),
            LayoutTableEvidence(
                page_number=1,
                bbox=(0.10, 0.55, 0.90, 0.80),
                column_bounds=_THREE_COLUMN_BOUNDS,
                rows=(
                    _three_column_row(("aa", "bb", "cc"), 0.55, 0.65),
                    _three_column_row(("35", "Lyon", "Inactive"), 0.65, 0.75),
                ),
            ),
        ),
    )
    raw = TextBlock(
        text=("Table A\n\naa bb cc 22 Paris Active\n\nSeparate results follow.\n\naa bb cc 35 Lyon Inactive"),
        page_number=1,
    )
    processed = ProcessedDocument(
        text_blocks=[raw.model_copy(deep=True)],
        raw_text_blocks=[raw],
        page_count=1,
    )

    result = await DeterministicTableNormalizer(FakeEvidenceProvider({1: evidence}, discovered={1})).normalize(
        Document(filename="plain-tables.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf"),
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert [row.table_title for row in rows] == ["Table A", None]


@pytest.mark.asyncio
async def test_repeated_identity_values_still_receive_distinct_row_ids():
    evidence = _one_page_table(
        (
            _three_column_row(("ID", "Type", "Description"), 0.20, 0.30),
            _three_column_row(("A", "Alpha", "First detail"), 0.30, 0.40),
            _three_column_row(("A", "Alpha", "Second detail"), 0.40, 0.50),
        )
    )
    markdown = "| ID | Type | Description |\n|---|---|---|\n| A | Alpha | First detail |\n| A | Alpha | Second detail |"

    _, result = await _normalize_one_page_table(
        markdown,
        evidence,
        filename="duplicate-identities.pdf",
    )

    rows = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert len(rows) == 2
    assert [cell.text for cell in rows[0].cells[:2]] == [cell.text for cell in rows[1].cells[:2]]
    assert rows[0].row_id != rows[1].row_id


@pytest.mark.asyncio
async def test_layout_discovery_normalizes_table_without_parser_markdown():
    evidence = _one_page_table(
        (
            _three_column_row(("aa", "bb", "cc"), 0.25, 0.35),
            _three_column_row(("22", "Paris", "Active"), 0.35, 0.45),
        )
    )
    raw = TextBlock(text="aa bb cc 22 Paris Active", page_number=1)
    processed = ProcessedDocument(
        text_blocks=[raw.model_copy(deep=True)],
        raw_text_blocks=[raw],
        page_count=1,
    )
    provider = FakeEvidenceProvider({1: evidence}, discovered={1})

    result = await DeterministicTableNormalizer(provider).normalize(
        Document(filename="plain-table.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf"),
        processed,
        TableReconstructionConfig(mode="automatic"),
    )

    [row] = [block.table_row for block in result.effective_text_blocks() if block.table_row is not None]
    assert [cell.text for cell in row.cells] == ["22", "Paris", "Active"]
    assert "the value “22” in column “aa”" in next(
        block.text for block in result.effective_text_blocks() if block.table_row is row
    )
