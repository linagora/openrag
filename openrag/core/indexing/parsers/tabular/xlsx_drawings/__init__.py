"""Objects drawn on worksheets and chart sheets: charts today, pictures next.

Both live in OOXML drawings (``xl/drawings/drawingN.xml``) and share how they
are found (``package``). Each kind has its own parser and renderer:

- ``charts``: classic chart part -> typed :class:`~.charts.Chart` model;
- ``chartex``: Office 2016 chart part (waterfall, treemap...) -> the same model;
- ``references``: values of series that carry no cache, read from the cells;
- ``render``: chart -> searchable text and chunk metadata;
- ``extract``: the per-workbook orchestration the XLSX parser calls.

Nothing here makes a workbook fail: an unreadable drawing or chart is skipped
and counted, and the worksheet tables are indexed as usual.
"""

from .extract import CHART_MODES, ChartLimits, TableCoverage, WorkbookCharts

__all__ = ["CHART_MODES", "ChartLimits", "TableCoverage", "WorkbookCharts"]
