"""
CSV parser checks:
    01 Basic output, identity, UTF-8 BOM and leading zeros
    02 Quoted commas, escaped quotes and multiline values
    03 Duplicate headers and empty cells
    04 Markdown escaping
    05 Reading a file without changing it
    06 Empty input
    07 Rejecting the wrong number of cells
    08 Rejecting an unclosed quote
    09 Allow for an explicit delimiter, ex : CsvParser(delimiter=";")
    10 Error message identifies the malformed record
    11 Reading many records from a file without never losing or reordering them
    12 Batch sizes, repeated headers, row order and final partial batches
    13 Lazy parsing: a later bad record does not prevent reading the first batch
    14 Input stream closes after completion, an error or an early stop
    15 Reject invalid batch sizes
    16 The async parse interface collects batches and preserves document metadata

Tests call the parser directly, not the upload/dispatcher or chunking pipeline.
"""

import csv
from contextlib import closing

import pytest
from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.document import Document

# we apply the asyncio marker to every async test in this file
pytestmark = pytest.mark.asyncio


async def test_01_normal_csv():
    document = Document(
        id="people-file",
        raw_bytes="id,name\n001,Ela\n002,Ali".encode("utf-8-sig"),
        metadata={"source": "example"},
    )

    result = await CsvParser().parse(document)
    assert result.document_id == "people-file"
    assert result.metadata == {"source": "example"}
    assert len(result.text_blocks) == 1
    assert result.text_blocks[0].block_type == "table"
    assert result.text_blocks[0].text == ("| id | name |\n| --- | --- |\n| 001 | Ela |\n| 002 | Ali |")


async def test_02_quoted_and_multiline_value():
    document = Document(text='id,note\n1,"Doe, John said ""hello""\nNext line"')
    result = await CsvParser().parse(document)
    assert result.text_blocks[0].text == ('| id | note |\n| --- | --- |\n| 1 | Doe, John said "hello"<br>Next line |')


async def test_03_duplicate_headers_and_empty_cells():
    document = Document(text="name,name,note\nAli,,\n,Bob,hello")
    result = await CsvParser().parse(document)
    assert result.text_blocks[0].text == (
        "| name | name | note |\n| --- | --- | --- |\n| Ali |  |  |\n|  | Bob | hello |"
    )


async def test_04_markdown_escaping():
    document = Document(text="id,note\n1,A|B & <tag>")
    result = await CsvParser().parse(document)
    assert result.text_blocks[0].text == ("| id | note |\n| --- | --- |\n| 1 | A&#124;B &amp; &lt;tag&gt; |")


async def test_05_reading_file_preserves_original(tmp_path):
    # pytest gives this test its own temporary directory.
    path = tmp_path / "people.csv"
    original = b"id,name\n001,Ali"
    path.write_bytes(original)
    result = await CsvParser().parse(Document(source_path=str(path)))
    assert result.text_blocks[0].text == "| id | name |\n| --- | --- |\n| 001 | Ali |"
    assert path.read_bytes() == original


async def test_06_empty_file():
    result = await CsvParser().parse(Document(raw_bytes=b""))
    assert result.text_blocks == []


async def test_07_inconsistent_column_count():
    document = Document(text="id,name\n1,Ali,extra")

    # a "passing" test means the bad record was rejected
    with pytest.raises(ValueError, match="Expected 2 cells, got 3"):
        await CsvParser().parse(document)


async def test_08_unclosed_quote():
    document = Document(text='id,name\n1,"Ali')
    with pytest.raises(csv.Error):
        await CsvParser().parse(document)


async def test_09_feature_explicit_delimiter():
    """Accept a delimiter option, keeping comma as the default."""
    document = Document(text='id;name\n001;"Doe; John"')
    result = await CsvParser(delimiter=";").parse(document)
    # the quoted semicolon belongs to the name and not to a new column
    assert result.text_blocks[0].text == "| id | name |\n| --- | --- |\n| 001 | Doe; John |"


async def test_10_feature_error_identifies_record():
    """Checking that the error clearly identifies the malformed CSV record."""
    # Record 1 is the header
    # Record 2 spans physical lines 2 and 3 because its quoted cell contains a newline
    # Record 3 has an extra cell and starts on the 4th physical line
    document = Document(text='id,note\n1,"first\nsecond"\n2,extra,cell')
    with pytest.raises(ValueError, match=r"(?i)\brecord 3\b"):
        await CsvParser().parse(document)


async def test_11_many_records_from_file(tmp_path):
    """Checking that every record in a larger CSV survives the parsing."""
    # create a csv on disk by writing records one at a time
    path = tmp_path / "many_records.csv"
    row_count = 10_000

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "name"])
        for number in range(row_count):
            writer.writerow([f"{number:06d}", f"Person {number}"])

    # give the parser a file path, and not preloaded text or bytes
    document = Document(source_path=str(path))
    result = await CsvParser().parse(document)

    # one complete Markdown table is returned
    assert len(result.text_blocks) == 1
    block = result.text_blocks[0]
    assert block.block_type == "table"
    lines = block.text.splitlines()
    assert lines[:2] == ["| id | name |", "| --- | --- |"]
    assert len(lines) == row_count + 2

    # check all records including their order and leading zeros
    for number, line in enumerate(lines[2:]):
        assert line == f"| {number:06d} | Person {number} |"


@pytest.mark.parametrize(
    ("row_count", "batch_size", "expected_sizes"),
    [(0, 2, [0]), (1, 2, [1]), (2, 2, [2]), (3, 2, [2, 1]), (4, 2, [2, 2]), (5, 2, [2, 2, 1]), (5, 3, [3, 2])],
)
async def test_12_batch_boundaries(row_count, batch_size, expected_sizes):
    text = "id,name\n" + "".join(f"{number:03d},Person {number}\n" for number in range(row_count))
    # collecting these tiny examples is convenient for assertions but not for large-file consumers
    batches = list(CsvParser(batch_size=batch_size).iter_batches(Document(text=text)))
    sizes = []
    rows = []
    for batch in batches:
        assert batch.block_type == "table"
        lines = batch.text.splitlines()
        assert lines[:2] == ["| id | name |", "| --- | --- |"]
        sizes.append(len(lines) - 2)
        rows.extend(lines[2:])
    assert sizes == expected_sizes
    assert rows == [f"| {number:03d} | Person {number} |" for number in range(row_count)]


async def test_13_batches_are_read_on_demand():
    #  quoted multiline cells still count as just one data row.
    document = Document(text='id,note\n1,"first\nsecond"\n2,ok\n3,extra,cell')
    with closing(CsvParser(batch_size=2).iter_batches(document)) as batches:
        first = next(batches)
        assert first.text == "| id | note |\n| --- | --- |\n| 1 | first<br>second |\n| 2 | ok |"
        # the malformed record is checked only when the next batch is requested
        with pytest.raises(ValueError, match=r"Record 4: Expected 2 cells, got 3"):
            next(batches)


@pytest.mark.parametrize("ending", ["complete", "error", "early_stop", "empty"])
async def test_14_input_stream_is_closed(tmp_path, monkeypatch, ending):
    path = tmp_path / "input.csv"
    text = "id,name\n1,Ali\n2,Bob"
    if ending == "error":
        text = "id,name\n1,Ali\n2,Bob,extra"
    elif ending == "empty":
        text = ""
    path.write_text(text, encoding="utf-8")
    parser = CsvParser(batch_size=1)
    document = Document(source_path=str(path))
    # we keep a reference to the real file stream so its closed state can be checked
    stream = parser._open_stream(document)
    monkeypatch.setattr(parser, "_open_stream", lambda document: stream)
    with closing(parser.iter_batches(document)) as batches:
        if ending == "empty":
            assert list(batches) == []
            assert stream.closed
        else:
            next(batches)
            assert not stream.closed
            if ending == "error":
                with pytest.raises(ValueError, match="Record 3"):
                    next(batches)
                assert stream.closed
            elif ending == "complete":
                assert len(list(batches)) == 1
                assert stream.closed
            # for early_stop, leaving the closing context just closes the suspended generator
    assert stream.closed


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, "2", None])
async def test_15_invalid_batch_size(batch_size):
    with pytest.raises(ValueError, match="positive integer"):
        CsvParser(batch_size=batch_size)


async def test_16_parse_collects_batches():
    document = Document(id="batched-file", text="id,name\n1,Ali\n2,Bob\n3,Ela", metadata={"source": "example"})
    result = await CsvParser(batch_size=2).parse(document)
    assert result.document_id == document.id
    assert result.metadata == document.metadata
    assert result.text_blocks[0].text == "| id | name |\n| --- | --- |\n| 1 | Ali |\n| 2 | Bob |"
    assert result.text_blocks[1].text == "| id | name |\n| --- | --- |\n| 3 | Ela |"
    assert len(result.text_blocks) == 2
