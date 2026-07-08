"""Tests for core.chunking.markdown_utils.

Mirrors components/indexer/chunker/test_chunking.py to verify behavior is
preserved through the move into core/.
"""

from __future__ import annotations

from core.chunking.markdown_utils import (
    MDElement,
    chunk_table,
    get_chunk_page_number,
    parse_markdown_table,
    span_inside,
    split_md_elements,
)


def _mock_length(text: str) -> int:
    """Estimate token count at ~4 chars per token (matches legacy tests)."""
    return len(text) // 4


class TestSplitMdElements:
    def test_simple_text_only(self):
        md = "This is a simple paragraph.\n\nAnother paragraph here."
        elems = split_md_elements(md)
        assert len(elems) == 1
        assert elems[0].type == "text"
        assert elems[0].content == md

    def test_single_table(self):
        md = (
            "Some text before.\n\n| Header 1 | Header 2 |\n|----------|----------|\n"
            "| Cell 1   | Cell 2   |\n| Cell 3   | Cell 4   |\n\nSome text after."
        )
        elems = split_md_elements(md)
        assert [e.type for e in elems] == ["text", "table", "text"]
        assert "Header 1" in elems[1].content

    def test_single_image(self):
        md = (
            "\nText before image.\n\n<image_description>\nA beautiful sunset over the ocean.\n"
            "</image_description>\n\nText after image."
        )
        elems = split_md_elements(md)
        assert [e.type for e in elems] == ["text", "image", "text"]
        assert "sunset" in elems[1].content

    def test_table_inside_image_description_is_ignored(self):
        md = (
            "\n<image_description>\nThis image contains a table:\n| Col 1 | Col 2 |\n"
            "|-------|-------|\n| A     | B     |\n</image_description>\n\n"
            "Outside table:\n| Real 1 | Real 2 |\n|--------|--------|\n| X      | Y      |\n"
        )
        elems = split_md_elements(md)
        tables = [e for e in elems if e.type == "table"]
        assert len(tables) == 1
        assert "Real 1" in tables[0].content

    def test_page_markers_with_table(self):
        md = (
            "text on page 1.\n[PAGE_1]\nText on page 2.\n\n"
            "| Header 1 | Header 2 |\n|----------|----------|\n| Data 1   | Data 2   |\n\n"
            "[PAGE_2]\nMore content.\n"
        )
        elems = split_md_elements(md)
        tables = [e for e in elems if e.type == "table"]
        assert len(tables) == 1
        assert tables[0].page_number == 2

    def test_page_markers_with_images(self):
        md = "\n[PAGE_1]\n[PAGE_2]\n<image_description>\nImage on page 3.\n</image_description>\n"
        elems = split_md_elements(md)
        images = [e for e in elems if e.type == "image"]
        assert len(images) == 1
        assert images[0].page_number == 3


class TestGetChunkPageNumber:
    def test_no_markers_returns_previous_page(self):
        result = get_chunk_page_number("Just some plain text content.", previous_chunk_ending_page=1)
        assert result == {"start_page": 1, "end_page": 1}

    def test_chunk_starts_with_marker(self):
        result = get_chunk_page_number("[PAGE_2]Content on page 3.", previous_chunk_ending_page=1)
        assert result == {"start_page": 3, "end_page": 3}

    def test_chunk_ends_with_marker(self):
        result = get_chunk_page_number("Content on page 1.[PAGE_1]", previous_chunk_ending_page=1)
        assert result == {"start_page": 1, "end_page": 1}

    def test_marker_in_middle(self):
        result = get_chunk_page_number("Start on page 1.[PAGE_1]End on page 2.", previous_chunk_ending_page=1)
        assert result == {"start_page": 1, "end_page": 2}


class TestChunkTable:
    def test_small_table_no_chunking(self):
        content = "| Name | Age |\n|------|-----|\n| John | 30 |\n| Jane | 25 |"
        elem = MDElement(type="table", content=content, page_number=1)
        chunks = chunk_table(elem, chunk_size=1000, length_function=_mock_length)
        assert len(chunks) == 1
        assert chunks[0].type == "table"
        assert chunks[0].page_number == 1
        assert "John" in chunks[0].content
        assert "Jane" in chunks[0].content

    def test_chunking_preserves_groups(self):
        header = "| Country | Strategy | Goals |"
        g1 = "| USA     | Cyber    | Goal 1 |\n|         |          | Goal 2 |\n|         |          | Goal 3 |"
        g2 = "| Mexico  | Defense  | Goal X |\n|         |          | Goal Y |\n|         |          | Goal Z |"
        table = f"{header}\n|----|----|----|\n{g1}\n{g2}\n"
        elem = MDElement(type="table", content=table, page_number=2)
        chunk_size = _mock_length(table) // 2
        chunks = chunk_table(elem, chunk_size=chunk_size, length_function=_mock_length)
        assert len(chunks) == 2
        assert all(c.type == "table" for c in chunks)
        assert all(header in c.content for c in chunks)
        assert "USA" in chunks[0].content

    def test_oversized_first_group_does_not_emit_header_only_chunk(self):
        """When the very first group is already larger than chunk_size, the
        old code flushed a header-only chunk (CodeRabbit #1)."""
        header = "| Country | Strategy | Goals |"
        g1 = "| USA     | Cyber    | Goal 1 |\n|         |          | Goal 2 |\n|         |          | Goal 3 |"
        g2 = "| Mexico  | Defense  | Goal X |"
        table = f"{header}\n|----|----|----|\n{g1}\n{g2}\n"
        # Tight budget: g1 alone already exceeds.
        chunks = chunk_table(
            MDElement(type="table", content=table, page_number=1), chunk_size=2, length_function=_mock_length
        )
        # No chunk may contain only the header.
        for c in chunks:
            body = c.content.replace(header, "").strip()
            assert body, f"header-only chunk emitted: {c.content!r}"

    def test_overlap_replays_only_last_row_not_full_group(self):
        """The docstring promises last-row overlap; the old code stored the
        whole previous group (CodeRabbit #1)."""
        header = "| Country | Strategy | Goal |"
        g1 = "| USA  | Cyber | first  |\n|      |       | second |\n|      |       | LAST_ROW_OF_G1 |"
        g2 = "| Mexico | Defense | only |"
        table = f"{header}\n|----|----|----|\n{g1}\n{g2}\n"
        # Force a split between g1 and g2.
        chunk_size = _mock_length(g1) + _mock_length(header)
        chunks = chunk_table(
            MDElement(type="table", content=table, page_number=1), chunk_size=chunk_size, length_function=_mock_length
        )
        assert len(chunks) >= 2
        second_chunk = chunks[1].content
        assert "LAST_ROW_OF_G1" in second_chunk, "last row should be replayed as overlap"
        # The earlier rows of g1 must NOT appear in the second chunk.
        assert "first" not in second_chunk
        assert "second" not in second_chunk


def test_md_element_repr_truncates_content():
    elem = MDElement(type="text", content="x" * 500, page_number=3)
    rendered = repr(elem)
    assert "type=text" in rendered
    assert "page_number=3" in rendered
    # Long content is truncated to <=100 chars + ellipsis.
    assert "x" * 200 not in rendered


def test_span_inside_helper():
    assert span_inside((10, 20), (5, 30)) is True
    assert span_inside((5, 30), (10, 20)) is False
    assert span_inside((10, 20), (10, 20)) is True


def test_parse_markdown_table_skips_blank_data_rows():
    """A pipe-only row (e.g. an extra blank `|`) yields no cells; it must be
    skipped without erroring or starting a phantom group."""
    header = "| Country | Goal |"
    delim = "|---------|------|"
    table = f"{header}\n{delim}\n| USA | A |\n|\n| Mexico | B |"
    headers, groups = parse_markdown_table(table)
    assert headers == [header, delim]
    # Two non-empty rows -> two groups (each row has a non-empty Domain).
    assert len(groups) == 2
