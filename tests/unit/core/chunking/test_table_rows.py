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
    assert all(chunk.metadata["page_end"] == 805 for chunk in chunks)
    assert all(chunk.metadata["source_fragments"] for chunk in chunks)
