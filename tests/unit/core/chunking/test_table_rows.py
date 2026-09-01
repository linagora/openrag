import re

from core.chunking.table_rows import chunk_table_legend, chunk_table_row
from core.models.document import (
    PageBoundaryDecision,
    SourceFragment,
    TableCellData,
    TableLegendData,
    TableLegendEntry,
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

    chunks = chunk_table_row(row, chunk_size=50, length_function=_words)

    assert len(chunks) > 1
    assert all(_words(chunk.text) <= 50 for chunk in chunks)
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

    assert "the first row (row 1)" in chunk.text
    assert "the value “L. 421-1” in column “Reference”" in chunk.text
    assert "the value “Proof of employment” in column “Requirement”" in chunk.text
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


def test_normal_table_rows_render_as_independent_natural_language_evidence():
    first = TableRowData(
        table_id="table-a",
        row_id="row-a-1",
        algorithm_version="adjacent-layout-v1",
        table_title="Table A",
        row_index=1,
        page_start=1,
        page_end=1,
        cells=[
            TableCellData(column_index=0, column_name="aa", text="22"),
            TableCellData(column_index=1, column_name="bb", text="Paris"),
            TableCellData(column_index=2, column_name="cc", text="Active"),
        ],
    )
    second = first.model_copy(
        update={
            "row_id": "row-a-2",
            "row_index": 2,
            "cells": [
                TableCellData(column_index=0, column_name="aa", text="35"),
                TableCellData(column_index=1, column_name="bb", text="Lyon"),
                TableCellData(column_index=2, column_name="cc", text="Inactive"),
            ],
        }
    )

    first_chunk = chunk_table_row(first, chunk_size=100, length_function=_words)[0]
    second_chunk = chunk_table_row(second, chunk_size=100, length_function=_words)[0]

    assert first_chunk.text == (
        "In table “Table A”, the first row (row 1) has the value “22” in column “aa”, "
        "the value “Paris” in column “bb”, and the value “Active” in column “cc”."
    )
    assert "the second row (row 2)" in second_chunk.text
    assert "the value “35” in column “aa”" in second_chunk.text
    assert first_chunk.metadata["row_index"] == 1
    assert first_chunk.metadata["table_content_kind"] == "row"

    query_terms = set(re.findall(r"\w+", "Table A first row aa 22".casefold()))
    scores = [
        len(query_terms & set(re.findall(r"\w+", chunk.text.casefold()))) for chunk in (first_chunk, second_chunk)
    ]
    assert scores[0] > scores[1]


def test_legend_is_a_separate_searchable_chunk():
    legend = TableLegendData(
        table_id="permits",
        algorithm_version="adjacent-layout-v1",
        table_title="ANNEXE",
        section_path=["Article Annexe 10", "ANNEXE"],
        page_number=1,
        entries=[
            TableLegendEntry(abbreviation="CST", meaning="carte de séjour temporaire"),
            TableLegendEntry(abbreviation="CSP", meaning="carte de séjour pluriannuelle"),
        ],
    )

    [chunk] = chunk_table_legend(legend, chunk_size=100, length_function=_words)

    assert "CST means “carte de séjour temporaire”" in chunk.text
    assert "CSP means “carte de séjour pluriannuelle”" in chunk.text
    assert chunk.metadata["table_content_kind"] == "legend"
    assert chunk.metadata["legend_abbreviations"] == ["CST", "CSP"]


def test_merged_and_empty_cells_do_not_shift_or_duplicate_values():
    row = TableRowData(
        table_id="merged",
        row_id="merged-row",
        algorithm_version="adjacent-layout-v1",
        row_index=1,
        page_start=1,
        page_end=1,
        cells=[
            TableCellData(
                column_index=0,
                column_name="Region",
                text="North",
                column_span=2,
            ),
            TableCellData(
                column_index=1,
                column_name="Area",
                covered_by=(1, 0),
            ),
            TableCellData(
                column_index=2,
                column_name="Owner",
                text="",
                explicit_empty=True,
            ),
        ],
    )

    [chunk] = chunk_table_row(row, chunk_size=100, length_function=_words)

    assert "the value “North” in columns “Region” and “Area”" in chunk.text
    assert chunk.text.count("North") == 1
    assert "no value in column “Owner”" in chunk.text


def test_oversized_identity_is_preserved_as_complete_text_instead_of_truncated():
    row = TableRowData(
        table_id="long-identity",
        row_id="long-identity-row",
        algorithm_version="adjacent-layout-v1",
        identity_columns=[0, 1, 2],
        page_start=1,
        page_end=1,
        cells=[
            TableCellData(
                column_index=0,
                column_name="Category",
                text="A very long professional residence permit category",
            ),
            TableCellData(column_index=1, column_name="Permit", text="CST salarié"),
            TableCellData(column_index=2, column_name="Reference", text="L. 421-1"),
            TableCellData(
                column_index=3,
                column_name="Supporting documents",
                text=" ".join(f"requirement-{index}" for index in range(40)),
            ),
        ],
    )

    chunks = chunk_table_row(row, chunk_size=30, length_function=_words)

    assert len(chunks) > 1
    assert all(_words(chunk.text) <= 30 for chunk in chunks)
    assert all("A very long professional residence permit category" in chunk.text for chunk in chunks)
    assert all("CST salarié" in chunk.text for chunk in chunks)
    assert all("L. 421-1" in chunk.text for chunk in chunks)
    assert all("has “professional" not in chunk.text for chunk in chunks)


def test_single_oversized_legend_definition_is_split_without_losing_its_meaning():
    meaning = " ".join(f"definition-{index}" for index in range(40))
    legend = TableLegendData(
        table_id="long-legend",
        algorithm_version="adjacent-layout-v1",
        table_title="Table A",
        page_number=1,
        entries=[
            TableLegendEntry(abbreviation="ABC", meaning=meaning),
        ],
    )

    chunks = chunk_table_legend(legend, chunk_size=16, length_function=_words)

    assert len(chunks) > 1
    assert all(_words(chunk.text) <= 16 for chunk in chunks)
    assert all("ABC means" in chunk.text for chunk in chunks)
    recovered = " ".join(re.search(r"ABC means “(.+)”\.", chunk.text).group(1) for chunk in chunks)
    assert recovered == meaning


def test_context_that_exceeds_chunk_size_is_emitted_as_bounded_row_parts():
    identity = " ".join(f"identity-{index}" for index in range(20))
    content = " ".join(f"content-{index}" for index in range(10))
    row = TableRowData(
        table_id="extreme",
        row_id="extreme-row",
        algorithm_version="adjacent-layout-v1",
        identity_columns=[0],
        page_start=1,
        page_end=1,
        cells=[
            TableCellData(column_index=0, column_name="Identity", text=identity),
            TableCellData(column_index=1, column_name="Content", text=content),
        ],
    )

    chunks = chunk_table_row(row, chunk_size=12, length_function=_words)

    assert len(chunks) > 1
    assert all(_words(chunk.text) <= 12 for chunk in chunks)
    recovered_content = " ".join(
        chunk.text.rsplit("\n\n", 1)[-1] for chunk in chunks if chunk.metadata["content_column"] == "Content"
    )
    recovered_identity = " ".join(
        chunk.text.rsplit("\n\n", 1)[-1] for chunk in chunks if chunk.metadata["content_column"] == "Identity"
    )
    assert recovered_content == content
    assert recovered_identity == identity
    assert all(chunk.metadata["row_id"] == "extreme-row" for chunk in chunks)


def test_legend_scope_that_exceeds_chunk_size_still_preserves_every_word():
    meaning = " ".join(f"meaning-{index}" for index in range(10))
    legend = TableLegendData(
        table_id="extreme-legend",
        algorithm_version="adjacent-layout-v1",
        table_title="A deliberately long table title",
        page_number=1,
        entries=[TableLegendEntry(abbreviation="XYZ", meaning=meaning)],
    )

    chunks = chunk_table_legend(legend, chunk_size=6, length_function=_words)

    assert len(chunks) > 1
    assert all("XYZ means" in chunk.text for chunk in chunks)
    assert all(_words(chunk.text) <= 6 for chunk in chunks)
    recovered = " ".join(re.search(r"XYZ means “(.+)”\.", chunk.text).group(1) for chunk in chunks)
    assert recovered == meaning


def test_oversized_row_repeats_explicit_empty_identity_context():
    row = TableRowData(
        table_id="empty-context",
        row_id="empty-context-row",
        algorithm_version="adjacent-layout-v1",
        identity_columns=[0, 1],
        page_start=1,
        page_end=1,
        cells=[
            TableCellData(column_index=0, column_name="Reference", text="A-1"),
            TableCellData(
                column_index=1,
                column_name="Owner",
                text="",
                explicit_empty=True,
            ),
            TableCellData(
                column_index=2,
                column_name="Details",
                text=" ".join(f"detail-{index}" for index in range(30)),
            ),
        ],
    )

    chunks = chunk_table_row(row, chunk_size=20, length_function=_words)

    assert len(chunks) > 1
    assert all(_words(chunk.text) <= 20 for chunk in chunks)
    assert all("“Owner” = empty" in chunk.text for chunk in chunks)
