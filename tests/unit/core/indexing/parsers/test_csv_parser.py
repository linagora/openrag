import csv
import pytest

from core.indexing.parsers.tabular.csv_parser import CsvParser
from core.models.document import Document


@pytest.mark.asyncio
async def test_normal_csv():
    # Arrange: create a document containing CSV bytes.
    document = Document(
        filename="people.csv",
        raw_bytes=b'id,name\n001,"Doe, Jane"\n002,Alice\n',
        metadata={"source": "example"},
    )

    # Act: call your parser directly.
    result = await CsvParser().parse(document)

    # Assert: check identity, metadata and the exact table.
    assert result.document_id == document.id
    assert result.metadata == {"source": "example"}
    assert len(result.text_blocks) == 1

    block = result.text_blocks[0]
    assert block.block_type == "table"
    assert block.text == (
        "| id | name |\n"
        "| --- | --- |\n"
        "| 001 | Doe, Jane |\n"
        "| 002 | Alice |"
    )


@pytest.mark.asyncio
async def test_multiline_value_and_empty_cell():
    document = Document(
        text='id,note\n1,"first line\nsecond line"\n2,\n'
    )

    result = await CsvParser().parse(document)

    assert result.text_blocks[0].text == (
        "| id | note |\n"
        "| --- | --- |\n"
        "| 1 | first line<br>second line |\n"
        "| 2 |  |"
    )


@pytest.mark.asyncio
async def test_empty_file():
    result = await CsvParser().parse(Document(raw_bytes=b""))

    assert result.text_blocks == []


@pytest.mark.asyncio
async def test_inconsistent_column_count():
    document = Document(text="id,name\n1,Alice,extra\n")

    with pytest.raises(ValueError, match="Expected 2 cells, got 3"):
        await CsvParser().parse(document)


@pytest.mark.asyncio
async def test_unclosed_quote():
    document = Document(text='id,name\n1,"Alice')

    with pytest.raises(csv.Error):
        await CsvParser().parse(document)


@pytest.mark.asyncio
async def test_reading_existing_file(tmp_path):
    # tmp_path is a temporary directory provided by pytest.
    path = tmp_path / "people.csv"
    original_bytes = b"id,name\n001,Alice\n"
    path.write_bytes(original_bytes)

    document = Document(
        filename="people.csv",
        source_path=str(path),
    )

    result = await CsvParser().parse(document)

    assert "| 001 | Alice |" in result.text_blocks[0].text

    # Reading the file must not change or delete it.
    assert path.read_bytes() == original_bytes