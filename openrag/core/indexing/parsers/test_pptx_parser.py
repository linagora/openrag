"""Tests for PptxParser._chart_to_markdown — regression for #388.

The old pptx_loader.py only returned from the ValueError branch when the
message contained "unsupported plot type"; any other ValueError fell through
and the function returned None implicitly, causing TypeError in the caller.
"""

import pytest

from .pptx_parser import PptxParser


class _RaisingCategories:
    """Fake plot whose .categories property raises ValueError."""

    def __init__(self, msg: str):
        self._msg = msg

    @property
    def categories(self):
        raise ValueError(self._msg)


class _Chart:
    """Minimal fake chart object for _chart_to_markdown unit tests."""

    def __init__(self, error_msg: str):
        self.has_title = False
        self.plots = [_RaisingCategories(error_msg)]
        self.series = []


class TestChartToMarkdown:
    def test_unsupported_plot_type_returns_placeholder(self):
        """'unsupported plot type' ValueError returns a non-empty placeholder string."""
        result = PptxParser._chart_to_markdown(_Chart("unsupported plot type"))
        assert isinstance(result, str)
        assert result.strip()

    def test_arbitrary_value_error_returns_placeholder(self):
        """Any other ValueError must also return a non-empty placeholder, not None.

        This is the regression: the old code had a bare else fall-through that
        returned None implicitly, raising TypeError when the caller concatenated
        the result into the slide markdown string.
        """
        result = PptxParser._chart_to_markdown(_Chart("some completely different error"))
        assert isinstance(result, str)
        assert result.strip()

    def test_general_exception_returns_placeholder(self):
        """A non-ValueError exception (e.g. AttributeError) also returns a placeholder."""

        class _BadChart:
            has_title = False

            @property
            def plots(self):
                raise AttributeError("no plots attribute")

            series = []

        result = PptxParser._chart_to_markdown(_BadChart())
        assert isinstance(result, str)
        assert result.strip()

    def test_return_value_is_never_none(self):
        """_chart_to_markdown must never return None regardless of the error type."""
        for msg in ["unsupported plot type", "something else", "", "re.error: group ref"]:
            result = PptxParser._chart_to_markdown(_Chart(msg))
            assert result is not None, f"Got None for error message: {msg!r}"
