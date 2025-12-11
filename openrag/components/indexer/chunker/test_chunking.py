# from .utils import split_md_elements

from components.indexer.chunker.utils import (
    MDElement,
    chunk_table,
    clean_markdown_table_spacing,
    get_chunk_page_number,
    split_md_elements,
)


class TestSplitMdElements:
    """Test suite for split_md_elements function."""

    def test_simple_text_only(self):
        """Test parsing markdown with only text content."""
        md_text = "This is a simple paragraph.\n\nAnother paragraph here."
        elements = split_md_elements(md_text)

        assert len(elements) == 1
        assert elements[0].type == "text"
        assert md_text == elements[0].content

    def test_single_table(self):
        """Test parsing a single markdown table."""

        md_text = "Some text before.\n\n| Header 1 | Header 2 |\n|----------|----------|\n| Cell 1   | Cell 2   |\n| Cell 3   | Cell 4   |\n\nSome text after."
        elements = split_md_elements(md_text)

        # Should have: text, table, text
        assert len(elements) == 3
        assert elements[0].type == "text"
        assert elements[1].type == "table"
        assert elements[2].type == "text"
        assert "Header 1" in elements[1].content

    def test_single_image(self):
        """Test parsing a single image description."""
        md_text = """
Text before image.

<image_description>
A beautiful sunset over the ocean.
</image_description>

Text after image."""
        elements = split_md_elements(md_text)

        assert len(elements) == 3
        assert elements[0].type == "text"
        assert elements[1].type == "image"
        assert elements[2].type == "text"
        assert "sunset" in elements[1].content

    def test_table_inside_image_description(self):
        """Test that tables inside image descriptions are ignored."""
        md_text = """
<image_description>
This image contains a table:
| Col 1 | Col 2 |
|-------|-------|
| A     | B     |
</image_description>

Outside table:
| Real 1 | Real 2 |
|--------|--------|
| X      | Y      |
"""
        elements = split_md_elements(md_text)

        # Should have: image, text, table
        assert len(elements) == 3
        assert elements[0].type == "image"
        assert elements[1].type == "text"
        assert elements[2].type == "table"
        # The table inside image should not be parsed separately
        table_elements = [e for e in elements if e.type == "table"]
        assert len(table_elements) == 1
        assert "Real 1" in table_elements[0].content

    def test_page_markers_with_table(self):
        """Test page number assignment for tables."""
        md_text = """text on page 1.
[PAGE_1]
Text on page 2.

| Header 1 | Header 2 |
|----------|----------|
| Data 1   | Data 2   |

[PAGE_2]
More content.
"""
        elements = split_md_elements(md_text)
        assert len(elements) == 3  # text, table, text

        table_elements = [e for e in elements if e.type == "table"]
        assert len(table_elements) == 1
        assert table_elements[0].page_number == 2

    def test_page_markers_with_images(self):
        """Test page number assignment for images."""
        md_text = """
[PAGE_1]
[PAGE_2]
<image_description>
Image on page 3.
</image_description>
"""
        elements = split_md_elements(md_text)

        image_elements = [e for e in elements if e.type == "image"]
        assert len(image_elements) == 1
        assert image_elements[0].page_number == 3


class TestGetChunkPageNumber:
    """Test suite for get_chunk_page_number function."""

    def test_no_page_markers(self):
        """Test chunk with no page markers."""
        chunk = "Just some plain text content."
        result = get_chunk_page_number(chunk, previous_chunk_ending_page=1)

        assert result["start_page"] == 1
        assert result["end_page"] == 1

    def test_starts_with_marker(self):
        """Test chunk starting with a page marker."""
        chunk = "[PAGE_2]Content on page 3."
        result = get_chunk_page_number(chunk, previous_chunk_ending_page=1)

        assert result["start_page"] == 3
        assert result["end_page"] == 3

    def test_ends_with_marker(self):
        """Test chunk ending with a page marker."""
        chunk = "Content on page 1.[PAGE_1]"
        result = get_chunk_page_number(chunk, previous_chunk_ending_page=1)

        assert result["start_page"] == 1
        assert result["end_page"] == 1

    def test_marker_in_middle(self):
        """Test chunk with marker in the middle."""
        chunk = "Start on page 1.[PAGE_1]End on page 2."
        result = get_chunk_page_number(chunk, previous_chunk_ending_page=1)

        assert result["start_page"] == 1
        assert result["end_page"] == 2


class TestCleanMarkdownTableSpacing:
    """Test suite for clean_markdown_table_spacing function."""

    def test_extra_spaces_in_cells(self):
        """Test trimming excessive spaces within cells."""
        table = "| Header 1    | Header 2     |\n|-------------|-------------|\n|  Cell 1   |   Cell 2    |"
        result = clean_markdown_table_spacing(table)

        assert (
            result
            == "| Header 1 | Header 2 |\n| ------------- | ------------- |\n| Cell 1 | Cell 2 |"
        )

    def test_inconsistent_spacing(self):
        """Test normalizing inconsistent spacing across rows."""
        table = "|Header1|Header2|\n|---|---|\n|  A  |B|"
        result = clean_markdown_table_spacing(table)

        assert result == "| Header1 | Header2 |\n| --- | --- |\n| A | B |"

    def test_empty_cells(self):
        """Test handling of empty cells."""
        table = "| Col1 | Col2 | Col3 |\n|------|------|------|\n| Data |      | More |\n|      | Data |      |"
        result = clean_markdown_table_spacing(table)

        assert (
            result
            == "| Col1 | Col2 | Col3 |\n| ------ | ------ | ------ |\n| Data |  | More |\n|  | Data |  |"
        )

    def test_multiline_spacing(self):
        """Test table with varying amounts of whitespace."""
        table = "|  A   |   B    |    C     |\n|------|--------|----------|\n|1|2|3|"
        result = clean_markdown_table_spacing(table)

        assert (
            result == "| A | B | C |\n| ------ | -------- | ---------- |\n| 1 | 2 | 3 |"
        )


class TestChunkTable:
    """Test suite for chunk_table function."""

    def mock_length_function(self, text):
        """Mock function that estimates token count (~4 chars per token)."""
        return len(text) // 4

    def test_small_table_no_chunking(self):
        """Test that a small table remains as a single chunk."""
        table_content = "| Name | Age |\n|------|-----|\n| John | 30 |\n| Jane | 25 |"
        table_element = MDElement(type="table", content=table_content, page_number=1)

        chunks = chunk_table(
            table_element, chunk_size=1000, length_function=self.mock_length_function
        )

        assert len(chunks) == 1
        assert chunks[0].type == "table"
        assert chunks[0].page_number == 1
        assert "John" in chunks[0].content
        assert "Jane" in chunks[0].content

    def test_chunking_preserves_groups(self):
        """Test that country groups are not split mid-group."""
        header = "| Country | Strategy | Goals |"
        g1 = (
            "| USA     | Cyber    | Goal 1 |\n"
            "|         |          | Goal 2 |\n"
            "|         |          | Goal 3 |"
        )

        g2 = (
            "| Mexico  | Defense  | Goal X |\n"
            "|         |          | Goal Y |\n"
            "|         |          | Goal Z |"
        )

        table = f"{header}\n|----|----|----|\n{g1}\n{g2}\n"
        table_element = MDElement(type="table", content=table, page_number=2)

        table_token_length = self.mock_length_function(table)
        chunk_size = table_token_length // 2  # to enforce chunking into 2 chunks

        # Force chunking with small chunk size
        chunks = chunk_table(
            table_element,
            chunk_size=chunk_size,
            length_function=self.mock_length_function,
        )

        assert len(chunks) == 2  # Should be chunked
        assert all(chunk.type == "table" for chunk in chunks)
        assert all(
            header in chunk.content for chunk in chunks
        )  # All table chunks should have the header part

        # check that groups are intact
        assert "USA" in chunks[0].content
        assert "Mexico" in chunks[1].content
