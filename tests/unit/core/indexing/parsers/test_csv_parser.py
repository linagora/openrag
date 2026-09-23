"""
Current results (tests 01 to 10 should pass):
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
    11 ...

The failed tests are not skipped or marked as failed and they describe useful new behaviors.
Tests call the parser directly, not the upload/dispatcher or chunking pipeline.
"""

import csv

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
    """Next task is toaccept a delimiter option, keeping comma as the default."""
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