from core.chunking.table_rows import chunk_table_row
from core.models.document import (
    PageBoundaryDecision,
    SourceFragment,
    TableCellData,
    TableRowData,
)


def _words(text: str) -> int:
    return len(text.split())


def test_oversized_cell_chunks_repeat_row_context_and_preserve_provenance():
    supporting_documents = " ".join(f"requirement-{index}." for index in range(100))
    row = TableRowData(
        table_id="table-1",
        row_id="row-1",
        algorithm_version="adjacent-layout-v1",
        table_title="Supporting documents",
        section_path=["Annex 10"],
        identity_columns=[0, 1, 2],
        page_start=803,
        page_end=805,
        cells=[
            TableCellData(column_index=0, column_name="Number", text="1"),
            TableCellData(column_index=1, column_name="Title", text="CST salarié"),
            TableCellData(column_index=2, column_name="Reference", text="L. 421-1"),
            TableCellData(
                column_index=3,
                column_name="Supporting documents",
                text=supporting_documents,
                source_fragments=[
                    SourceFragment(
                        source_block_index=1,
                        page_number=804,
                        char_start=0,
                        char_end=len(supporting_documents),
                        text_start=0,
                        text_end=len(supporting_documents),
                    )
                ],
            ),
        ],
        boundary_decisions=[
            PageBoundaryDecision(
                previous_page=803,
                next_page=804,
                same_table_confidence=0.97,
                row_continuation_confidence=0.98,
                decision="merged",
                reason="test",
            )
        ],
    )

    chunks = chunk_table_row(row, chunk_size=30, length_function=_words)

    assert len(chunks) > 1
    assert all(_words(chunk.text) <= 30 for chunk in chunks)
    assert all("CST salarié" in chunk.text for chunk in chunks)
    assert all("L. 421-1" in chunk.text for chunk in chunks)
    assert all("row-1" not in chunk.text for chunk in chunks)
    assert all("table-1" not in chunk.text for chunk in chunks)
    assert all(chunk.page_number == 804 for chunk in chunks)
    assert all(chunk.metadata["page_start"] == 803 for chunk in chunks)
    assert all(chunk.metadata["page_end"] == 805 for chunk in chunks)
    assert all(chunk.metadata["source_fragments"] for chunk in chunks)


def test_unsplit_row_keeps_internal_ids_in_metadata_only():
    row = TableRowData(
        table_id="table-opaque-hash",
        row_id="row-opaque-hash",
        algorithm_version="adjacent-layout-v1",
        table_title="Residence permits",
        section_path=["Annex"],
        identity_columns=[0],
        page_start=2,
        page_end=3,
        cells=[
            TableCellData(column_index=0, column_name="Reference", text="L. 421-1"),
            TableCellData(
                column_index=1,
                column_name="Requirement",
                text="Proof of employment",
                source_fragments=[
                    SourceFragment(
                        source_block_index=2,
                        page_number=3,
                        char_start=0,
                        char_end=len("Proof of employment"),
                        text_start=0,
                        text_end=len("Proof of employment"),
                    )
                ],
            ),
        ],
    )

    [chunk] = chunk_table_row(row, chunk_size=100, length_function=_words)

    assert "Reference: L. 421-1" in chunk.text
    assert "Requirement: Proof of employment" in chunk.text
    assert "table-opaque-hash" not in chunk.text
    assert "row-opaque-hash" not in chunk.text
    assert chunk.metadata["table_id"] == "table-opaque-hash"
    assert chunk.metadata["row_id"] == "row-opaque-hash"
    assert chunk.metadata["page_start"] == 2
    assert chunk.metadata["page_end"] == 3
    assert chunk.page_number == 3


def test_split_content_uses_page_from_overlapping_source_fragment():
    page_two = " ".join(f"page-two-{index}." for index in range(24))
    page_three = " ".join(f"page-three-{index}." for index in range(24))
    supporting_documents = f"{page_two}\n\n{page_three}"
    page_three_start = len(page_two) + 2
    row = TableRowData(
        table_id="table-1",
        row_id="row-1",
        algorithm_version="adjacent-layout-v1",
        table_title="Supporting documents",
        identity_columns=[0],
        page_start=1,
        page_end=3,
        cells=[
            TableCellData(column_index=0, column_name="Reference", text="L. 421-1"),
            TableCellData(
                column_index=1,
                column_name="Supporting documents",
                text=supporting_documents,
                source_fragments=[
                    SourceFragment(
                        source_block_index=1,
                        page_number=2,
                        char_start=0,
                        char_end=len(page_two),
                        text_start=0,
                        text_end=len(page_two),
                    ),
                    SourceFragment(
                        source_block_index=2,
                        page_number=3,
                        char_start=0,
                        char_end=len(page_three),
                        text_start=page_three_start,
                        text_end=len(supporting_documents),
                    ),
                ],
            ),
        ],
    )

    chunks = chunk_table_row(row, chunk_size=15, length_function=_words)
    page_three_chunks = [chunk for chunk in chunks if "page-three" in chunk.text and "page-two" not in chunk.text]

    assert page_three_chunks
    assert all(chunk.page_number == 3 for chunk in page_three_chunks)
    assert all(
        any(fragment["page_number"] == 3 for fragment in chunk.metadata["source_fragments"])
        for chunk in page_three_chunks
    )
