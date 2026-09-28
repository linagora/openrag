"""Checking the connection between CSV batches and the existing chunker."""

from contextlib import closing

import pytest
from core.chunking.structured_section import StructuredSectionChunker
from core.indexing.csv_chunking import iter_csv_chunks
from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.document import Document


def make_chunker():
    # count every word for predictable tests without even loading a tokenizer.
    # this is a 'test' counter (and not the production token counter)
    return StructuredSectionChunker(
        chunk_size=50,
        min_tokens=0,
        max_tokens=100,
        inline_threshold=0,
        length_function=lambda text: len(text.split()),
    )


def test_records_headers_and_metadata_survive_chunking():
    # rows long enough to exercise table splitting within a single batch
    text = "id,note\n"
    for number in range(1, 10):
        text += f"{number:03d}," + "detail " * 40 + "\n"

    document = Document(
        id="csv-example",
        partition="test-partition",
        text=text,
        metadata={"source": "people.csv"},
    )

    # collecting output is fine for this small test for now
    groups = list(
        iter_csv_chunks(
            document,
            CsvParser(batch_size=4),
            make_chunker(),
        )
    )

    # 9records produce parser batches of 4, 4 and 1
    assert len(groups) == 3
    assert all(groups)

    # 1 parser batch can become several retrieval chunks
    assert len(groups[0]) > 1

    chunks = [chunk for group in groups for chunk in group]

    # chunk numbering must not restart for each batch
    assert [chunk.chunk_index for chunk in chunks] == list(
        range(len(chunks))
    )

    for batch_index, group in enumerate(groups, start=1):
        for chunk in group:
            assert chunk.document_id == document.id
            assert chunk.partition == document.partition
            assert chunk.metadata["source"] == "people.csv"
            assert chunk.metadata["csv_batch_index"] == batch_index
            assert "| id | note |" in chunk.text

    # all the records must appear + the table chunker may repeat rows
    # intentionally as overlap, so we do not require exactly one occurrence
    for number in range(1, 10):
        assert any(
            f"| {number:03d} |" in chunk.text
            for chunk in chunks
        )


def test_empty_csv_produces_no_chunk_batches():
    groups = list(
        iter_csv_chunks(
            Document(text=""),
            CsvParser(batch_size=2),
            make_chunker(),
        )
    )
    assert groups == []


def test_later_invalid_record_is_checked_on_demand():
    document = Document(
        text="id,note\n1,ok\n2,ok\n3,extra,cell"
    )

    with closing(
        iter_csv_chunks(
            document,
            CsvParser(batch_size=2),
            make_chunker(),
        )
    ) as groups:
        # the very first valid CSV batch can already be chunked at this point
        first_chunks = next(groups)
        assert first_chunks

        # now requesting the next batch discovers the malformed record
        with pytest.raises(ValueError, match="Record 4"):
            next(groups)
