"""
Tests for text sanitization utilities.
"""

from .text_sanitizer import (
    clean_markdown_table_spacing,
    sanitize_extracted_text,
    sanitize_text,
)


class TestSanitizeText:
    """Test suite for sanitize_text function."""

    def test_basic_text_unchanged(self):
        """Test that normal text passes through unchanged."""
        text = "Hello world. This is a test."
        result = sanitize_text(text)
        assert result == text

    def test_remove_excessive_spaces(self):
        """Test removal of excessive spaces."""
        text = "Hello    world   with    many     spaces"
        result = sanitize_text(text)
        assert result == "Hello world with many spaces"

    def test_remove_tabs(self):
        """Test conversion of tabs to single space."""
        text = "Hello\t\tworld\twith\t\t\ttabs"
        result = sanitize_text(text)
        assert result == "Hello world with tabs"

    def test_limit_consecutive_newlines(self):
        """Test limiting consecutive newlines."""
        text = "Line 1\n\n\n\n\nLine 2"
        result = sanitize_text(text, max_consecutive_newlines=2)
        assert result == "Line 1\n\nLine 2"

    def test_remove_control_characters(self):
        """Test removal of control characters."""
        text = "Hello\x00\x01\x02world"
        result = sanitize_text(text)
        assert result == "Helloworld"

    def test_remove_zero_width_characters(self):
        """Test removal of zero-width characters."""
        text = "Hello\u200b\u200c\u200dworld"
        result = sanitize_text(text)
        assert result == "Helloworld"

    def test_preserve_newlines_and_tabs_when_configured(self):
        """Test that newlines are preserved."""
        text = "Line 1\nLine 2\nLine 3"
        result = sanitize_text(text, normalize_whitespace=False)
        assert "\n" in result

    def test_trim_leading_trailing_whitespace(self):
        """Test removal of leading and trailing whitespace."""
        text = "   Hello world   \n\n"
        result = sanitize_text(text)
        assert result == "Hello world"

    def test_normalize_line_breaks(self):
        """Test normalization of different line break styles."""
        text = "Line 1\r\nLine 2\rLine 3\nLine 4"
        result = sanitize_text(text)
        assert result == "Line 1\nLine 2\nLine 3\nLine 4"

    def test_remove_leading_spaces_from_lines(self):
        """Test removal of leading spaces from each line."""
        text = "Line 1\n   Line 2 with spaces\n  Line 3"
        result = sanitize_text(text)
        assert result == "Line 1\nLine 2 with spaces\nLine 3"

    def test_remove_trailing_spaces_from_lines(self):
        """Test removal of trailing spaces from each line."""
        text = "Line 1   \nLine 2 with spaces   \nLine 3  "
        result = sanitize_text(text)
        assert result == "Line 1\nLine 2 with spaces\nLine 3"

    def test_empty_string(self):
        """Test handling of empty string."""
        result = sanitize_text("")
        assert result == ""

    def test_complex_mixed_issues(self):
        """Test handling of multiple issues simultaneously."""
        text = "  Hello    world\x00\t\twith\n\n\n\nmany\u200bissues   \n"
        result = sanitize_text(text)
        assert result == "Hello world with\n\nmanyissues"

    def test_unicode_normalization(self):
        """Test unicode normalization."""
        # Decomposed form: é as e + combining acute accent
        text_decomposed = "café\u0301"  # cafe with combining accent
        result = sanitize_text(text_decomposed, normalize_unicode=True)
        # Should normalize to composed form
        assert "é" in result or result == "café"

    def test_disable_whitespace_normalization(self):
        """Test that whitespace normalization can be disabled."""
        text = "Hello    world"
        result = sanitize_text(text, normalize_whitespace=False)
        assert "    " in result

    def test_disable_control_char_removal(self):
        """Test that control character removal can be disabled."""
        text = "Hello\x00world"
        result = sanitize_text(text, remove_control_chars=False)
        assert "\x00" in result

    def test_no_max_consecutive_newlines(self):
        """Test unlimited consecutive newlines."""
        text = "Line 1\n\n\n\n\nLine 2"
        result = sanitize_text(text, max_consecutive_newlines=0)
        assert result.count("\n") == 5


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


class TestSanitizeExtractedText:
    """Test suite for sanitize_extracted_text convenience function."""

    def test_applies_all_default_sanitizations(self):
        """Test that all default sanitizations are applied."""
        text = "  Hello    world\x00\t\twith\n\n\n\nmany\u200bissues   \n"
        result = sanitize_extracted_text(text)

        # Should have normalized spaces
        assert "    " not in result
        # Should have removed control chars
        assert "\x00" not in result
        # Should have removed zero-width chars
        assert "\u200b" not in result
        # Should have limited newlines to 2
        assert "\n\n\n" not in result
        # Should be trimmed
        assert not result.startswith(" ")
        assert not result.endswith(" ")

    def test_basic_extraction_scenario(self):
        """Test a realistic extraction scenario."""
        # Simulating text extracted from a PDF with various artifacts
        text = """
        Document Title
        
        
        
        This is a paragraph with    excessive    spaces and some\t\ttabs.
        
        Another paragraph here.
        
        
        Some \x00control\x01 characters\x02 too.
        
        And zero-width\u200bspaces.
        """

        result = sanitize_extracted_text(text)

        # Check that text is cleaned properly
        assert "    " not in result  # No excessive spaces
        assert "\t\t" not in result  # No multiple tabs
        assert "\x00" not in result  # No control chars
        assert "\u200b" not in result  # No zero-width spaces
        # Should have at most 2 consecutive newlines
        assert "\n\n\n" not in result
        # Should still have content structure
        assert "Document Title" in result
        assert "paragraph" in result
