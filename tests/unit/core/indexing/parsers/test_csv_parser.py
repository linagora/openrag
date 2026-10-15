"""
CSV parser checks:
    01 Basic output, identity, UTF-8 BOM and leading zeros
    02 Quoted commas, escaped quotes and multiline values
    03 Duplicate headers and empty cells
    04 Markdown escaping
    05 Reading a file without changing it
    06 Empty input
    07 Skipping the wrong number of cells
    08 Skipping an unclosed quote
    09 Allow for an explicit delimiter, ex : CsvParser(delimiter=";")
    10 Malformed records do not discard valid records
    11 Reading many records from a file without never losing or reordering them
    12 Batch sizes, repeated headers, row order and final partial batches
    13 Lazy parsing: a later bad record does not prevent reading the first batch
    14 Input stream closes after completion, an error or an early stop
    15 Reject invalid batch sizes
    16 The async parse interface collects batches and preserves document metadata
    ...

Tests call the parser directly, not the upload/dispatcher or chunking pipeline.
"""

import csv
from contextlib import closing

import pytest
from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.document import Document
from core.utils.exceptions import ConfigError, NoIndexableContentError

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


@pytest.mark.parametrize("encoding", ["cp1252", "latin-1"])
async def test_csv_decodes_non_utf8_sources(encoding, tmp_path):
    text = "id,name\n1,André\n2,Zoë"
    path = tmp_path / f"people-{encoding}.csv"
    path.write_bytes(text.encode(encoding))

    from_path = await CsvParser().parse(Document(source_path=str(path)))
    from_bytes = await CsvParser().parse(Document(raw_bytes=text.encode(encoding)))

    assert from_path.text_blocks[0].text == from_bytes.text_blocks[0].text
    assert "André" in from_path.text_blocks[0].text


async def test_csv_path_sample_can_end_inside_a_utf8_character(tmp_path):
    sample_size = 64 * 1024
    prefix = b"id,note\n1,"
    path = tmp_path / "boundary.csv"
    path.write_bytes(prefix + b"x" * (sample_size - len(prefix) - 1) + "é".encode() + b"\n")

    result = await CsvParser().parse(Document(source_path=str(path)))

    assert result.text_blocks[0].text.endswith("é |")


async def test_06_empty_file():
    result = await CsvParser().parse(Document(raw_bytes=b""))
    assert result.text_blocks == []


async def test_07_inconsistent_column_count():
    document = Document(text="id,name\n1,Ali,extra")

    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(document)

    assert exc_info.value.extra["skipped_rows"] == 1


async def test_08_unclosed_quote():
    document = Document(text='id,name\n1,"Ali')

    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(document)

    assert exc_info.value.extra["skipped_rows"] == 1


async def test_09_feature_explicit_delimiter():
    """Accept a delimiter option, keeping comma as the default."""
    document = Document(text='id;name\n001;"Doe; John"')
    result = await CsvParser(delimiter=";").parse(document)
    # the quoted semicolon belongs to the name and not to a new column
    assert result.text_blocks[0].text == "| id | name |\n| --- | --- |\n| 001 | Doe; John |"


async def test_delimiter_is_sniffed_when_not_explicit():
    document = Document(text='id;amount;note\n1;3,5;"valid; value"')

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == "| id | amount | note |\n| --- | --- | --- |\n| 1 | 3,5 | valid; value |"


async def test_semicolon_delimiter_is_sniffed_with_trailing_empty_cells():
    document = Document(text="a;b\n1;2;\n3;4;\n")

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"
    assert result.text_blocks[0].metadata["csv_columns"] == ["a", "b"]


async def test_10_feature_error_identifies_record():
    document = Document(text='id,note\n1,"first\nsecond"\n2,extra,cell')
    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text.endswith("| 1 | first<br>second |")
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 1


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
    [(1, 2, [1]), (2, 2, [2]), (3, 2, [2, 1]), (4, 2, [2, 2]), (5, 2, [2, 2, 1]), (5, 3, [3, 2])],
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
    expected_ranges = []
    for first_row in range(2, row_count + 2, batch_size):
        expected_ranges.append((first_row, min(first_row + batch_size - 1, row_count + 1)))
    assert [(batch.metadata["csv_row_start"], batch.metadata["csv_row_end"]) for batch in batches] == expected_ranges
    assert all(batch.metadata["csv_columns"] == ["id", "name"] for batch in batches)
    assert sizes == expected_sizes
    assert rows == [f"| {number:03d} | Person {number} |" for number in range(row_count)]


async def test_13_batches_are_read_on_demand():
    #  quoted multiline cells still count as just one data row.
    document = Document(text='id,note\n1,"first\nsecond"\n2,ok\n3,extra,cell')
    with closing(CsvParser(batch_size=2).iter_batches(document)) as batches:
        first = next(batches)
        assert first.text == "| id | note |\n| --- | --- |\n| 1 | first<br>second |\n| 2 | ok |"
        # The malformed record is consumed only when the next batch is requested.
        with pytest.raises(StopIteration):
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
                assert list(batches) == []
                assert stream.closed
            elif ending == "complete":
                assert len(list(batches)) == 1
                assert stream.closed
            # for early_stop, leaving the closing context just closes the suspended generator
    assert stream.closed


@pytest.mark.parametrize("batch_size", [0, -1, True, 1.5, "2", None])
async def test_15_invalid_batch_size(batch_size):
    with pytest.raises(ConfigError, match="positive integer"):
        CsvParser(batch_size=batch_size)


async def test_16_parse_collects_batches():
    document = Document(id="batched-file", text="id,name\n1,Ali\n2,Bob\n3,Ela", metadata={"source": "example"})
    result = await CsvParser(batch_size=2).parse(document)
    assert result.document_id == document.id
    assert result.metadata == document.metadata
    assert result.text_blocks[0].text == "| id | name |\n| --- | --- |\n| 1 | Ali |\n| 2 | Bob |"
    assert result.text_blocks[1].text == "| id | name |\n| --- | --- |\n| 3 | Ela |"
    assert len(result.text_blocks) == 2


async def test_malformed_records_are_skipped_without_losing_valid_rows():
    document = Document(text='id,name\n1,Alice\n2,Bob,\n3,"x"y\n4,Dana\n')

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == ("| id | name |\n| --- | --- |\n| 1 | Alice |\n| 2 | Bob |\n| 4 | Dana |")
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 1
    assert result.text_blocks[0].metadata["csv_row_numbers"] == [2, 3, 5]


async def test_missing_and_extra_trailing_empty_cells_are_normalized():
    document = Document(text="id,name,note\n1,Alice\n2,Bob,,\n3,Cam,ok")

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == (
        "| id | name | note |\n| --- | --- | --- |\n| 1 | Alice |  |\n| 2 | Bob |  |\n| 3 | Cam | ok |"
    )
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 0
    assert result.text_blocks[0].metadata["csv_row_numbers"] == [2, 3, 4]


async def test_header_without_indexable_rows_raises():
    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(Document(text="id,name\n1,Alice,unexpected\n"))

    assert exc_info.value.code == "NO_INDEXABLE_CONTENT"
    assert exc_info.value.extra == {"skipped_rows": 1}


async def test_unclosed_quote_in_header_raises():
    with pytest.raises(NoIndexableContentError) as exc_info:
        await CsvParser().parse(Document(text='a,"b\n1,2\n'))

    assert exc_info.value.code == "NO_INDEXABLE_CONTENT"
    assert exc_info.value.extra == {"skipped_rows": 1}


async def test_trailing_empty_header_names_are_removed():
    result = await CsvParser().parse(Document(text="a,b,\n1,2\n"))

    assert result.text_blocks[0].text == "| a | b |\n| --- | --- |\n| 1 | 2 |"
    assert result.text_blocks[0].metadata["csv_columns"] == ["a", "b"]


async def test_cell_larger_than_python_csv_default_limit_is_accepted():
    value = "x" * (256 * 1024)

    result = await CsvParser().parse(Document(text=f"id,note\n1,{value}\n"))

    assert value in result.text_blocks[0].text
    assert result.text_blocks[0].metadata["csv_bad_rows"] == 0


async def test_late_cp1252_character_is_not_replaced(tmp_path):
    path = tmp_path / "late-cp1252.csv"
    prefix = "x" * (70 * 1024)
    path.write_bytes(f"id,note\n1,{prefix} café\n".encode("cp1252"))

    result = await CsvParser().parse(Document(source_path=str(path)))

    assert "café" in result.text_blocks[0].text
    assert "\ufffd" not in result.text_blocks[0].text
