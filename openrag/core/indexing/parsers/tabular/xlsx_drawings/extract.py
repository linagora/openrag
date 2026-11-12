"""Charts of one workbook: located and parsed up front, rendered on demand.

The parser emits each sheet's charts right after that sheet's tables
(:meth:`WorkbookCharts.before_sheet`), then the charts of sheets that had no
table, such as dashboards and chart sheets (:meth:`WorkbookCharts.remaining`).

Charts are parsed before any is rendered because a chart without cached values
reads them from worksheet cells, and all such reads of the workbook are
batched into one read per referenced sheet (``references.py``). A chart that
cannot be read is skipped and counted; it never fails the workbook.
"""

from __future__ import annotations

import re
import zipfile
from bisect import bisect_left
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any
from xml.etree.ElementTree import ParseError

from core.utils.logging import get_logger
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from .chartex import parse_chartex
from .charts import Chart, DataRef, Plot, Series, parse_chart
from .package import PART_READ_ERRORS, ChartPlacement, _relationship_id, locate_charts
from .references import (
    CellRange,
    RowReader,
    first_cell_formats,
    parse_reference,
    resolve_ranges,
    series_values,
)
from .render import RenderedChart, ValueFormatter, complete_chart, render_chart, render_text_boxes
from .shapes import ShapeText, drawn_texts

logger = get_logger()


#: What chart extraction does with charts; ``loader.xlsx_charts``.
#: - ``full``: every chart with all its data, hidden sources included (the
#:   chart shows that data to anyone who opens the file); the text says when
#:   data comes from a hidden source.
#: - ``without_hidden_data``: every chart, but values, labels and names read
#:   from a hidden worksheet, row or column are left out, and the text says so.
#: - ``off``: no chart is read.
CHART_MODES = ("full", "without_hidden_data", "off")


@dataclass(frozen=True)
class ChartLimits:
    max_charts: int
    max_part_bytes: int
    max_points: int
    max_batch_bytes: int
    # A chart whose data is already indexed as worksheet tables repeats it only
    # up to this many points; past it, it keeps a summary and points to them.
    max_points_from_tables: int = 200
    # Chart parts read across the workbook; charts past it are skipped.
    max_total_bytes: int | None = None


@dataclass(frozen=True)
class TableCoverage:
    """An indexed worksheet table a chart range overlaps."""

    table_id: str
    table_range: str
    # The chart range lies entirely inside this table.
    contains: bool


@dataclass(frozen=True)
class _Entry:
    placement: ChartPlacement
    chart: Chart
    unresolved: int
    source_tables: tuple[str, ...] = ()
    # Ranges of those tables, when every plotted range lies inside one of them.
    covering_tables: tuple[str, ...] = ()
    # Hidden worksheets the chart reads, and whether their data was left out.
    hidden_sources: tuple[str, ...] = ()
    hidden_data_withheld: bool = False
    # A chart that needed no cell read is rendered as soon as it is parsed, so
    # a workbook of many large cached charts never holds more than their text.
    rendered: RenderedChart | None = None


#: Turns a series formula into the single range it reads, or None.
Resolver = Callable[[str | None], CellRange | None]

_NAME = re.compile(r"^(?:(?:'(?P<quoted>(?:[^']|'')+)'|(?P<plain>[^'!\[\]]+)|\[0\])!)?(?P<name>[^\s!'()]+)$")


def make_resolver(defined_names: dict[tuple[int | None, str], str], sheet_indexes: dict[str, int]) -> Resolver:
    """Resolve plain references, then defined names (sheet scope first).

    A name defined as another name, a union or a formula stays unresolved.
    """

    def resolve(formula: str | None) -> CellRange | None:
        cell_range = parse_reference(formula)
        if cell_range is not None or not formula:
            return cell_range
        match = _NAME.match(formula.strip())
        if match is None:
            return None
        sheet = match["quoted"].replace("''", "'") if match["quoted"] else match["plain"]
        name = match["name"].casefold()
        target = None
        if sheet is not None and sheet in sheet_indexes:
            target = defined_names.get((sheet_indexes[sheet], name))
        target = target or defined_names.get((None, name))
        return parse_reference(target.lstrip("=")) if target else None

    return resolve


# Excel writes every Office 2016 chart reference as a hidden name of this form,
# defined in workbook.xml: ``_xlchart.v1.0`` -> ``Sheet1!$B$4:$D$14``.
_INTERNAL_NAME = re.compile(r"^_xlchart\.", re.IGNORECASE)


def _shown_references(chart: Chart, resolve: Resolver) -> Chart:
    """Replace Excel's internal chart names with the ranges they stand for.

    ``_xlchart.v1.0`` means nothing to a reader; ``Sheet1!$C$4:$C$15`` does,
    and it is what the hidden-sheet check and table linking need to see.
    Names a user defined (``Sales2025``) are kept: they carry meaning.
    """

    def shown(formula: str | None) -> str | None:
        if not formula or not _INTERNAL_NAME.match(formula.strip()):
            return formula
        cell_range = resolve(formula)
        return cell_range.reference if cell_range is not None else formula

    def dimension(reference: DataRef | None) -> DataRef | None:
        return replace(reference, formula=shown(reference.formula)) if reference is not None else None

    plots = tuple(
        replace(
            plot,
            category_header_ref=shown(plot.category_header_ref),
            series=tuple(
                replace(
                    series,
                    name_ref=shown(series.name_ref),
                    categories=dimension(series.categories),
                    values=dimension(series.values),
                    sizes=dimension(series.sizes),
                )
                for series in plot.series
            ),
        )
        for plot in chart.plots
    )
    return replace(chart, plots=plots, title_ref=shown(chart.title_ref))


def _source_ranges(chart: Chart, resolve: Resolver) -> list[CellRange]:
    """Every range the chart plots: categories, values and bubble sizes."""
    ranges = []
    for series in chart.series:
        for reference in (series.categories, series.values, series.sizes):
            cell_range = resolve(reference.formula) if reference is not None else None
            if cell_range is not None:
                ranges.append(cell_range)
    return ranges


def _uncached(chart: Chart, resolve: Resolver) -> list[CellRange]:
    ranges = []
    if chart.title is None and chart.title_ref:
        ranges.append(resolve(chart.title_ref))
    for plot in chart.plots:
        if plot.category_header is None and plot.category_header_ref:
            ranges.append(resolve(plot.category_header_ref))
    for series in chart.series:
        if series.name is None and series.name_ref:
            ranges.append(resolve(series.name_ref))
        for reference in (series.categories, series.values, series.sizes):
            if reference is not None and reference.needs_cells:
                ranges.append(resolve(reference.formula))
    return [cell_range for cell_range in ranges if cell_range is not None]


#: Hidden rows and hidden columns of a worksheet, by sheet name.
HiddenCells = Callable[[str], tuple[frozenset[int], frozenset[int]]]


def _filled(
    chart: Chart,
    resolved: dict[CellRange, list[list[str]]],
    hidden_cells: HiddenCells,
    resolve: Resolver,
    *,
    drop_hidden_cells: bool,
) -> tuple[Chart, int]:
    """Put resolved cell values into the chart; count series left without values.

    A chart plotting visible cells only (Excel's default) leaves out hidden
    and filtered rows and hidden columns, as Excel did when it drew the chart.
    """
    unresolved = 0

    def text_of(formula: str | None) -> str | None:
        """A title or series name: read even from a hidden cell, as Excel does."""
        cell_range = resolve(formula)
        grid = resolved.get(cell_range) if cell_range is not None else None
        cells = series_values(cell_range, grid) if grid is not None else ()
        return " ".join(cell for cell in cells if cell) or None

    def values_of(formula: str | None, positions: frozenset[int]) -> tuple[tuple[str, ...] | None, int | None]:
        """Return the series values and, when only part was read, the range's size.

        Points are left out by the series' own decision (``positions``); only
        a hidden label level of a two-dimensional range is dropped here.
        """
        cell_range = resolve(formula)
        grid = resolved.get(cell_range) if cell_range is not None else None
        if grid is None:
            return None, None
        hidden_rows, hidden_columns = hidden_cells(cell_range.sheet) if drop_hidden_cells else ((), ())
        vertical = cell_range.max_row - cell_range.min_row >= cell_range.max_column - cell_range.min_column
        values = series_values(
            cell_range,
            grid,
            hidden_rows=frozenset() if vertical else frozenset(hidden_rows),
            hidden_columns=frozenset(hidden_columns) if vertical else frozenset(),
        )
        values = _without(values, positions) if positions else values
        rows = cell_range.max_row - cell_range.min_row + 1
        columns = cell_range.max_column - cell_range.min_column + 1
        partial = len(grid) < rows or (bool(grid) and len(grid[0]) < columns)
        return values, max(rows, columns) if partial else None

    def fill(reference: DataRef | None, positions: frozenset[int]) -> DataRef | None:
        if reference is None or not reference.needs_cells:
            return reference
        values, declared = values_of(reference.formula, positions)
        return replace(reference, values=values, declared_points=declared) if values is not None else reference

    def series_of(series: Series) -> Series:
        nonlocal unresolved
        name = series.name
        if name is None and series.name_ref:
            name = text_of(series.name_ref)
        positions = _series_hidden_points(series, resolve, hidden_cells) if drop_hidden_cells else frozenset()
        filled = replace(
            series,
            name=name,
            categories=fill(series.categories, positions),
            values=fill(series.values, positions),
            sizes=fill(series.sizes, positions),
        )
        if filled.values is not None and filled.values.needs_cells:
            unresolved += 1
        return filled

    plots = tuple(
        replace(
            plot,
            series=tuple(series_of(series) for series in plot.series),
            category_header=plot.category_header
            if plot.category_header is not None or not plot.category_header_ref
            else text_of(plot.category_header_ref),
        )
        for plot in chart.plots
    )
    title = chart.title if chart.title is not None or not chart.title_ref else text_of(chart.title_ref)
    return replace(chart, title=title, plots=plots), unresolved


def _intersects(cell_range: CellRange, hidden_rows: frozenset[int], hidden_columns: frozenset[int]) -> bool:
    """Whether a range touches a hidden row or column; walks the smaller side."""

    def touches(low: int, high: int, hidden: frozenset[int]) -> bool:
        if high - low + 1 < len(hidden):
            return any(index in hidden for index in range(low, high + 1))
        return any(low <= index <= high for index in hidden)

    return touches(cell_range.min_row, cell_range.max_row, hidden_rows) or touches(
        cell_range.min_column, cell_range.max_column, hidden_columns
    )


def _hidden_points(cell_range: CellRange, hidden_rows: frozenset[int], hidden_columns: frozenset[int]) -> set[int]:
    """Positions of a range's points that sit on a hidden row or column.

    Points run along the range's longer side, as :func:`series_values` reads
    them; a one-cell-wide range on a hidden column (or row) hides every point.
    """
    vertical = cell_range.max_row - cell_range.min_row >= cell_range.max_column - cell_range.min_column
    if vertical:
        start, end, along = cell_range.min_row, cell_range.max_row, hidden_rows
        low, high, across = cell_range.min_column, cell_range.max_column, hidden_columns
    else:
        start, end, along = cell_range.min_column, cell_range.max_column, hidden_columns
        low, high, across = cell_range.min_row, cell_range.max_row, hidden_rows
    length = end - start + 1
    if low == high and low in across:
        return set(range(length))
    if length < len(along):
        return {index for index in range(length) if start + index in along}
    return {index - start for index in along if start <= index <= end}


def _point_length(cell_range: CellRange) -> int:
    return max(cell_range.max_row - cell_range.min_row, cell_range.max_column - cell_range.min_column) + 1


def _hidden_across(cell_range: CellRange, hidden_rows: frozenset[int], hidden_columns: frozenset[int]) -> bool:
    """Whether a two-dimensional range (several label levels) has a hidden level."""
    vertical = cell_range.max_row - cell_range.min_row >= cell_range.max_column - cell_range.min_column
    if vertical:
        low, high, across = cell_range.min_column, cell_range.max_column, hidden_columns
    else:
        low, high, across = cell_range.min_row, cell_range.max_row, hidden_rows
    return low < high and any(low <= index <= high for index in across)


def _series_hidden_points(series: Series, resolve: Resolver, hidden_cells: HiddenCells) -> frozenset[int]:
    """The points of a series Excel leaves out: one decision per point.

    A point is left out when any of its cells (category, value, bubble size)
    is on a hidden row or column, and then from every dimension at once, so
    categories and values stay paired even when their ranges are offset.
    """
    found: set[int] = set()
    for reference in (series.categories, series.values, series.sizes):
        cell_range = resolve(reference.formula) if reference is not None else None
        if cell_range is not None:
            rows, columns = hidden_cells(cell_range.sheet)
            found |= _hidden_points(cell_range, frozenset(rows), frozenset(columns))
    return frozenset(found)


def _without(values: tuple[str, ...], positions: frozenset[int]) -> tuple[str, ...]:
    return tuple(value for index, value in enumerate(values) if index not in positions)


def _mappable(reference: DataRef | None, resolve: Resolver) -> bool:
    """Whether cached points match their cells one to one (or come from cells)."""
    if reference is None or reference.values is None:
        return True
    cell_range = resolve(reference.formula)
    return cell_range is None or len(reference.values) == _point_length(cell_range)


def _apply_policy(
    chart: Chart,
    resolve: Resolver,
    hidden_sheets: frozenset[str],
    hidden_cells: HiddenCells,
    *,
    withhold: bool,
    sheet: str | None = None,
) -> tuple[Chart, tuple[str, ...]]:
    """Find the hidden worksheets a chart reads and, if asked, withhold their data.

    Cached points on hidden rows and columns are dropped whenever Excel would
    not draw them (the chart plots visible cells only), and always when hidden
    data is withheld. Returns the chart and the hidden worksheets it reads.
    """
    sources: dict[str, None] = {}
    drop_hidden_cells = withhold or chart.plot_visible_only

    def on_hidden_sheet(formula: str | None) -> bool:
        cell_range = resolve(formula)
        if cell_range is not None and cell_range.sheet in hidden_sheets:
            sources[cell_range.sheet] = None
            return True
        return False

    def hidden_text(formula: str | None) -> bool:
        """A title, series name or header withheld: on a hidden sheet, row or column."""
        # A hidden sheet is recorded in every mode; only withholding acts on it.
        on_hidden = on_hidden_sheet(formula)
        if not withhold:
            return False
        if on_hidden:
            return True
        cell_range = resolve(formula)
        if cell_range is None:
            return False
        hidden_rows, hidden_columns = hidden_cells(cell_range.sheet)
        return _intersects(cell_range, frozenset(hidden_rows), frozenset(hidden_columns))

    def dimension(reference: DataRef | None, positions: frozenset[int]) -> DataRef | None:
        if reference is None:
            return None
        if on_hidden_sheet(reference.formula):
            return replace(reference, values=(), withheld=True) if withhold else reference
        cell_range = resolve(reference.formula)
        if not drop_hidden_cells or not reference.values:
            return reference
        if cell_range is not None:
            rows, columns = hidden_cells(cell_range.sheet)
            if not _mappable(reference, resolve) or _hidden_across(cell_range, frozenset(rows), frozenset(columns)):
                if positions or _intersects(cell_range, frozenset(rows), frozenset(columns)):
                    # A cache that cannot be matched to its cells is dropped:
                    # the visible cells are read instead, so no hidden label
                    # reaches the index and points stay paired.
                    return replace(reference, values=None)
                return reference
        return replace(reference, values=_without(reference.values, positions)) if positions else reference

    def hidden_or_unknown_text(formula: str | None) -> bool:
        return hidden_text(formula) or (withhold and bool(formula) and resolve(formula) is None)

    def labels_of(series: Series, values: DataRef | None, positions: frozenset[int]) -> DataRef | None:
        """Data labels follow their points: dropped with hidden points, withheld
        with withheld values or a label range on a hidden sheet."""
        labels = series.labels
        if labels is None:
            return None
        hidden_range = hidden_or_unknown_text(labels.formula)
        hidden_points = {index for index, formula in series.label_refs if hidden_or_unknown_text(formula)}
        if (values is not None and values.withheld) or hidden_range:
            return None
        label_range = resolve(labels.formula)
        if withhold and label_range is not None and labels.values:
            # A label read from a hidden cell is blanked; its point may stay.
            rows, columns = hidden_cells(label_range.sheet)
            hidden_points |= _hidden_points(label_range, frozenset(rows), frozenset(columns))
        if hidden_points and labels.values:
            labels = replace(labels, values=tuple("" if i in hidden_points else v for i, v in enumerate(labels.values)))
        if not drop_hidden_cells or not positions or not labels.values:
            return labels
        if not _mappable(series.values, resolve):
            # Points cannot be matched to cells: no label can be placed safely.
            return None
        return replace(labels, values=_without(labels.values, positions))

    def series_of(series: Series) -> Series:
        # A withheld name loses its reference too, so it is never read from the cell.
        hidden_name = hidden_text(series.name_ref)
        positions = _series_hidden_points(series, resolve, hidden_cells) if drop_hidden_cells else frozenset()
        values = dimension(series.values, positions)
        return replace(
            series,
            name=None if hidden_name else series.name,
            name_ref=None if hidden_name else series.name_ref,
            categories=dimension(series.categories, positions),
            values=values,
            sizes=dimension(series.sizes, positions),
            labels=labels_of(series, values, positions),
            label_refs=(),
        )

    def drawn(shape: ShapeText) -> bool:
        """A text box on the chart is kept unless withheld: hidden, or linked to
        a hidden cell. A link to a hidden sheet is reported in every mode."""
        if shape.textlink and hidden_or_unknown_text(_qualified(shape.textlink, sheet)):
            return False
        return not (withhold and shape.hidden)

    def totals_of(plot) -> tuple[int, ...]:
        """Waterfall totals are positions: they move with the points left out."""
        if not drop_hidden_cells or not plot.total_points or not plot.series:
            return plot.total_points
        first = plot.series[0]
        if not _mappable(first.values, resolve) or not _mappable(first.categories, resolve):
            return plot.total_points
        hidden = sorted(_series_hidden_points(first, resolve, hidden_cells))
        return tuple(
            position - bisect_left(hidden, position) for position in plot.total_points if position not in set(hidden)
        )

    def plot_of(plot):
        hidden_header = hidden_text(plot.category_header_ref)
        return replace(
            plot,
            series=tuple(series_of(series) for series in plot.series),
            category_header=None if hidden_header else plot.category_header,
            category_header_ref=None if hidden_header else plot.category_header_ref,
            total_points=totals_of(plot),
        )

    plots = tuple(plot_of(plot) for plot in chart.plots)
    if hidden_text(chart.title_ref):
        chart = replace(chart, title=None, title_ref=None)
    annotations = tuple(shape for shape in chart.annotations if drawn(shape))
    return replace(chart, plots=plots, annotations=annotations), tuple(sources)


def _qualified(link: str, sheet: str | None) -> str | None:
    """A text box's cell link with its sheet: ``$A$1`` on ``Sales`` is ``'Sales'!$A$1``."""
    link = link.lstrip("=").strip()
    if "!" in link or not sheet:
        return link or None
    return "'" + sheet.replace("'", "''") + "'!" + link


def _first_cell(cell_range: CellRange) -> CellRange:
    return CellRange(
        cell_range.sheet, cell_range.min_row, cell_range.min_column, cell_range.min_row, cell_range.min_column
    )


def _unformatted(reference: DataRef | None) -> bool:
    """Numbers whose file states no format: Excel shows them as their cells do."""
    return (
        reference is not None
        and reference.numeric
        and not reference.withheld
        and reference.format_code in (None, "", "General")
    )


def _format_probes(chart: Chart, resolve: Resolver) -> tuple[list[CellRange], list[CellRange]]:
    """Cells whose number format decides how the chart's numbers read.

    Numbers with no format of their own (a cache saying ``General``, or no
    cache at all) are shown by Excel with their cells' format. Return the
    probes of categories, whose date format rewrites every row, and those of
    values, whose percentage or currency format only adds a display line.
    """
    categories, values = [], []
    for series in chart.series:
        # Uncached categories come from the cells as text, dates included.
        if series.categories is not None and series.categories.values:
            cell_range = resolve(series.categories.formula) if _unformatted(series.categories) else None
            if cell_range is not None:
                categories.append(_first_cell(cell_range))
        for reference in (series.values, series.sizes):
            cell_range = resolve(reference.formula) if _unformatted(reference) else None
            if cell_range is not None:
                values.append(_first_cell(cell_range))
    return list(dict.fromkeys(categories)), list(dict.fromkeys(values))


def _header_cell(plot: Plot, resolve: Resolver, hidden_cells: HiddenCells, withhold: bool) -> CellRange | None:
    """The header cell of a plot's categories, when the layout shows it is one.

    Excel names a series from the cell above its values in a table with a
    header row; the cell above the categories in that same row is then the
    category column's header (``Month``). Only that layout is trusted: every
    named series must sit right below its name, in line with the categories.
    Rows work the same way, the header to the left.
    """
    if plot.category_axis_title or plot.category_header is not None or plot.category_header_ref:
        return None
    categories = next((series.categories for series in plot.series if series.categories is not None), None)
    if categories is None or categories.withheld:
        return None
    category_range = resolve(categories.formula)
    if category_range is None:
        return None
    named = 0
    by_column = category_range.min_column == category_range.max_column and category_range.min_row > 1
    by_row = category_range.min_row == category_range.max_row and category_range.min_column > 1
    for series in plot.series:
        if not series.name_ref:
            continue
        name = resolve(series.name_ref)
        values = resolve(series.values.formula) if series.values is not None else None
        if name is None or values is None or name.area != 1 or {name.sheet, values.sheet} != {category_range.sheet}:
            return None
        by_column = by_column and (
            name.min_column == values.min_column and name.min_row == values.min_row - 1 == category_range.min_row - 1
        )
        by_row = by_row and (
            name.min_row == values.min_row and name.min_column == values.min_column - 1 == category_range.min_column - 1
        )
        named += 1
    if not named or not (by_column or by_row):
        return None
    row, column = (
        (category_range.min_row - 1, category_range.min_column)
        if by_column
        else (category_range.min_row, category_range.min_column - 1)
    )
    if withhold:
        hidden_rows, hidden_columns = hidden_cells(category_range.sheet)
        if row in hidden_rows or column in hidden_columns:
            return None
    return CellRange(category_range.sheet, row, column, row, column)


def _with_header_refs(chart: Chart, resolve: Resolver, hidden_cells: HiddenCells, withhold: bool) -> Chart:
    """Point each plot at its category header cell, to be read with its values."""
    plots = []
    for plot in chart.plots:
        cell = _header_cell(plot, resolve, hidden_cells, withhold)
        plots.append(replace(plot, category_header_ref=cell.reference) if cell is not None else plot)
    return replace(chart, plots=tuple(plots))


@dataclass(frozen=True)
class _Lookups:
    """Cells a chart needs beyond its cache: number formats, and header labels."""

    formats: tuple[CellRange, ...] = ()
    headers: tuple[CellRange, ...] = ()
    # Category formats can turn every category into a date; the others only
    # change the chart's heading, so the chart can be rendered before them.
    rewrites_rows: bool = False

    def __bool__(self) -> bool:
        return bool(self.formats or self.headers)


@dataclass(frozen=True)
class _Deferred:
    """A chart rendered with placeholders, completed once its cells are read."""

    entry: _Entry


@dataclass(frozen=True)
class _LookedUp:
    formats: dict[CellRange, str]
    texts: dict[CellRange, list[list[str]]]


def _with_lookups(
    chart: Chart, resolve: Resolver, found: _LookedUp, hidden_cells: HiddenCells, withhold: bool
) -> Chart:
    """Give the chart its cells' number formats and its category headers."""
    from openpyxl.styles.numbers import is_date_format

    def formatted(reference: DataRef | None, *, dates: bool) -> DataRef | None:
        """Categories take a date format; values any other (percent, currency).

        A value series of dates keeps its numbers: rows are rendered before
        value formats are known, so a value format must never change them.
        """
        cell_range = resolve(reference.formula) if _unformatted(reference) else None
        code = found.formats.get(_first_cell(cell_range)) if cell_range is not None else None
        if not code or code == "General" or is_date_format(code) != dates:
            return reference
        return replace(reference, format_code=code)

    def series_of(series: Series) -> Series:
        return replace(
            series,
            categories=formatted(series.categories, dates=True),
            values=formatted(series.values, dates=False),
            sizes=formatted(series.sizes, dates=False),
        )

    def plot_of(plot: Plot) -> Plot:
        header = None
        cell = _header_cell(plot, resolve, hidden_cells, withhold)
        grid = found.texts.get(cell) if cell is not None else None
        if grid and grid[0]:
            header = grid[0][0][:_MAX_HEADER] or None
        return replace(
            plot,
            series=tuple(series_of(series) for series in plot.series),
            category_header=header if header is not None else plot.category_header,
        )

    return replace(chart, plots=tuple(plot_of(plot) for plot in chart.plots))


def _slim(chart: Chart) -> Chart:
    """What completing a rendered chart needs: one value per series, no points."""

    def sample(reference: DataRef | None) -> DataRef | None:
        if reference is None or not reference.values:
            return reference
        first = next((value for value in reference.values if value), None)
        return replace(reference, values=(first,) if first is not None else ())

    def series_of(series: Series) -> Series:
        return replace(
            series,
            categories=sample(series.categories),
            values=sample(series.values),
            sizes=sample(series.sizes),
            labels=None,
        )

    return replace(chart, plots=tuple(replace(plot, series=tuple(map(series_of, plot.series))) for plot in chart.plots))


def _held_points(chart: Chart) -> int:
    return sum(
        len(reference.values or ())
        for series in chart.series
        for reference in (series.categories, series.values, series.sizes, series.labels)
        if reference is not None
    )


# Points of the charts kept in memory while the cells they look up are read.
# Charts past it are dropped and parsed again once the cells are known, so
# memory stays bounded however many large charts a workbook holds.
_MAX_HELD_POINTS = 100_000
# A header cell is a label; a long text in it is cut, not copied in full.
_MAX_HEADER = 256


class WorkbookCharts:
    def __init__(
        self,
        source,
        *,
        limits: ChartLimits,
        workbook_filename: str,
        read_rows: RowReader,
        cell_text: Callable[[Any], str],
        describe_format: Callable[[Any, str], dict | None] | None = None,
        hidden_cells: HiddenCells | None = None,
        source_tables: Callable[[CellRange], list[TableCoverage]] | None = None,
        hidden_sheets: frozenset[str] = frozenset(),
        mode: str = "full",
    ) -> None:
        if mode not in CHART_MODES:
            raise ValueError(f"Unknown chart mode {mode!r}")
        self._limits = limits
        self._workbook_filename = workbook_filename
        self._emitted = 0
        self._bytes_read = 0
        if hasattr(source, "seek"):
            source.seek(0)
        with zipfile.ZipFile(source) as archive:
            # Text boxes are not charts: read in every mode, ``off`` included.
            inventory = locate_charts(
                archive,
                max_charts=limits.max_charts,
                max_part_bytes=limits.max_part_bytes,
                max_total_bytes=limits.max_total_bytes,
                read_charts=mode != "off",
            )
            self.skipped = list(inventory.skipped)
            self._formatter = ValueFormatter(
                date1904=inventory.date1904,
                **({"describe_format": describe_format} if describe_format else {}),
            )
            resolve = make_resolver(inventory.defined_names, inventory.sheet_indexes)
            hidden_cells = hidden_cells or (lambda sheet: ((), ()))
            self._withhold = mode == "without_hidden_data"
            self._texts = self._text_boxes(inventory, resolve, hidden_sheets, hidden_cells, withhold=mode != "full")
            self._source_tables = source_tables
            self._pending: list[_Entry] = []
            # Charts whose number formats or header labels sit in cells are
            # finished once those cells are read in bounded sheet windows.
            slots: list[_Entry | _Deferred | tuple[ChartPlacement, tuple[Chart, tuple[str, ...]] | None]] = []
            wanted_formats: list[CellRange] = []
            wanted_headers: list[CellRange] = []
            held = 0
            for placement in inventory.placements:
                loaded = self._load(archive, placement, resolve, hidden_sheets, hidden_cells, count_budget=True)
                if loaded is None:
                    continue
                chart, hidden_sources = loaded
                lookups = self._lookups(chart, resolve, hidden_cells)
                wanted_formats.extend(lookups.formats)
                wanted_headers.extend(lookups.headers)
                if not lookups or not lookups.rewrites_rows:
                    # Headers and value formats change only the heading: the
                    # chart is rendered now, and completed once they are read.
                    entry = self._finish(
                        placement, chart, hidden_sources, resolve, hidden_cells, deferred=bool(lookups)
                    )
                    slots.append(_Deferred(entry) if lookups else entry)
                    continue
                points = _held_points(chart)
                keep = held + points <= _MAX_HELD_POINTS
                held += points if keep else 0
                slots.append((placement, loaded if keep else None))
            found = _LookedUp(
                first_cell_formats(wanted_formats, read_rows) if wanted_formats else {},
                resolve_ranges(wanted_headers, read_rows, cell_text, max_cells=1) if wanted_headers else {},
            )
            for slot in slots:
                if isinstance(slot, _Entry):
                    self._pending.append(slot)
                    continue
                if isinstance(slot, _Deferred):
                    self._pending.append(self._complete(slot.entry, resolve, found, hidden_cells))
                    continue
                placement, loaded = slot
                loaded = loaded or self._load(
                    archive, placement, resolve, hidden_sheets, hidden_cells, count_budget=False
                )
                if loaded is not None:
                    self._pending.append(self._finish(placement, *loaded, resolve, hidden_cells, found=found))
        # Charts that store references read their cells in bounded windows.
        waiting = [entry for entry in self._pending if entry.rendered is None]
        wanted = [cell_range for entry in waiting for cell_range in _uncached(entry.chart, resolve)]
        resolved = resolve_ranges(wanted, read_rows, cell_text, max_cells=limits.max_points) if wanted else {}
        for index, entry in enumerate(self._pending):
            if entry.rendered is None:
                filled, unresolved = _filled(
                    entry.chart,
                    resolved,
                    hidden_cells,
                    resolve,
                    drop_hidden_cells=self._withhold or entry.chart.plot_visible_only,
                )
                self._pending[index] = replace(entry, chart=filled, unresolved=unresolved)
        if self.skipped:
            logger.bind(filename=workbook_filename).warning(
                f"{len(self.skipped)} XLSX chart part(s) skipped: {self.skipped[0]}"
            )

    def _load(
        self,
        archive: zipfile.ZipFile,
        placement: ChartPlacement,
        resolve: Resolver,
        hidden_sheets: frozenset[str],
        hidden_cells: HiddenCells,
        *,
        count_budget: bool,
    ) -> tuple[Chart, tuple[str, ...]] | None:
        """Parse a chart; return it with the hidden worksheets it reads."""
        chart = self._parse(archive, placement, count_budget=count_budget)
        if chart is None:
            return None
        chart = _shown_references(chart, resolve)
        # Withholding first means hidden data is never even read from the cells.
        chart, hidden_sources = _apply_policy(
            chart, resolve, hidden_sheets, hidden_cells, withhold=self._withhold, sheet=placement.sheet_name
        )
        if _uncached(chart, resolve):
            # The chart reads its cells anyway: its header cells join that read.
            chart = _with_header_refs(chart, resolve, hidden_cells, self._withhold)
        return chart, hidden_sources

    def _lookups(self, chart: Chart, resolve: Resolver, hidden_cells: HiddenCells) -> _Lookups:
        headers = (_header_cell(plot, resolve, hidden_cells, self._withhold) for plot in chart.plots)
        categories, values = _format_probes(chart, resolve)
        return _Lookups(
            tuple(categories + values), tuple(cell for cell in headers if cell is not None), bool(categories)
        )

    def _complete(self, entry: _Entry, resolve: Resolver, found: _LookedUp, hidden_cells: HiddenCells) -> _Entry:
        """Apply the cells a chart looked up: to its model, or to its rendered text."""
        chart = _with_lookups(entry.chart, resolve, found, hidden_cells, self._withhold)
        if entry.rendered is None:
            return replace(entry, chart=chart)
        rendered = complete_chart(entry.rendered, chart, self._formatter, self._limits.max_batch_bytes)
        return replace(entry, rendered=rendered, chart=replace(chart, plots=()))

    def _finish(
        self,
        placement: ChartPlacement,
        chart: Chart,
        hidden_sources: tuple[str, ...],
        resolve: Resolver,
        hidden_cells: HiddenCells,
        *,
        found: _LookedUp | None = None,
        deferred: bool = False,
    ) -> _Entry:
        """Describe the chart and render it if it needs nothing more from the cells.

        A ``deferred`` chart still waits for its header labels and value
        formats: it keeps placeholders and a slim model until :meth:`_complete`.
        """
        if found is not None:
            chart = _with_lookups(chart, resolve, found, hidden_cells, self._withhold)
        entry = self._entry(placement, chart, hidden_sources, resolve)
        if not _uncached(chart, resolve):
            # Nothing to read: count series left unresolved (unions, names
            # defined as formulas), render, and drop the model.
            _, unresolved = _filled(chart, {}, hidden_cells, resolve, drop_hidden_cells=False)
            entry = replace(entry, unresolved=unresolved)
            rendered = self._render_entry(entry, deferred=deferred)
            entry = replace(entry, rendered=rendered, chart=_slim(chart) if deferred else replace(chart, plots=()))
        return entry

    def _parse(self, archive: zipfile.ZipFile, placement: ChartPlacement, *, count_budget: bool = True) -> Chart | None:
        try:
            info = archive.getinfo(placement.chart_path)
        except KeyError:
            self.skipped.append(f"Chart {placement.chart_path} is missing from the workbook")
            return None
        if info.file_size > self._limits.max_part_bytes:
            self.skipped.append(f"Chart {placement.chart_path} exceeds the {self._limits.max_part_bytes}-byte limit")
            return None
        budget = self._limits.max_total_bytes
        if count_budget:
            if budget is not None and self._bytes_read + info.file_size > budget:
                self.skipped.append(f"Chart {placement.chart_path} is past the {budget}-byte workbook chart budget")
                return None
            self._bytes_read += info.file_size
        try:
            data = archive.read(placement.chart_path)
        except PART_READ_ERRORS as exc:
            self.skipped.append(f"Chart {placement.chart_path} could not be read ({type(exc).__name__})")
            return None
        try:
            root = fromstring(data)
            chart = parse_chartex(root) if placement.format == "chartex" else parse_chart(root)
        except (ParseError, DefusedXmlException, ValueError) as exc:
            self.skipped.append(f"Chart {placement.chart_path} could not be read ({type(exc).__name__})")
            return None
        if placement.user_shapes:
            reference = root.find("{*}userShapes")
            rel_id = _relationship_id(reference) if reference is not None else None
            path = dict(placement.user_shapes).get(rel_id) if rel_id else None
            if path is not None:
                chart = replace(chart, annotations=self._drawn_on(archive, path, count_budget))
        return chart

    def _drawn_on(self, archive: zipfile.ZipFile, path: str, count_budget: bool) -> tuple[ShapeText, ...]:
        """Text boxes drawn on a chart; a part that cannot be read only loses them."""
        try:
            size = archive.getinfo(path).file_size
        except KeyError:
            return ()
        budget = self._limits.max_total_bytes
        if size > self._limits.max_part_bytes or (
            count_budget and budget is not None and self._bytes_read + size > budget
        ):
            self.skipped.append(f"Chart drawing {path} exceeds the chart size limits")
            return ()
        if count_budget:
            self._bytes_read += size
        try:
            return drawn_texts(fromstring(archive.read(path)))
        except (*PART_READ_ERRORS, ParseError, DefusedXmlException, ValueError) as exc:
            self.skipped.append(f"Chart drawing {path} could not be read ({type(exc).__name__})")
            return ()

    def _entry(self, placement: ChartPlacement, chart: Chart, hidden_sources, resolve: Resolver) -> _Entry:
        """Describe a chart's links to the indexed tables before it is rendered."""
        ranges = _source_ranges(chart, resolve)
        coverage = [self._source_tables(cell_range) for cell_range in ranges] if self._source_tables else []
        tables = tuple(dict.fromkeys(table.table_id for found in coverage for table in found))
        # The chart's data is already indexed when each plotted range lies
        # inside a visible table; a hidden source is never "already indexed".
        # Every plotted dimension must come from such a range: literal values
        # or a reference that cannot be resolved were never indexed, and text
        # data labels are the chart's own, so those keep the chart's tables.
        dimensions = [
            reference
            for series in chart.series
            for reference in (series.categories, series.values, series.sizes)
            if reference is not None
        ]
        complete = all(resolve(reference.formula) is not None for reference in dimensions) and not any(
            series.labels is not None and any(series.labels.values or ()) for series in chart.series
        )
        covered = (
            bool(ranges)
            and complete
            and not hidden_sources
            and all(any(table.contains for table in found) for found in coverage)
        )
        covering = (
            tuple(dict.fromkeys(table.table_range for found in coverage for table in found if table.contains))
            if covered
            else ()
        )
        return _Entry(
            placement,
            chart,
            0,
            tables,
            covering,
            hidden_sources,
            self._withhold and bool(hidden_sources),
        )

    def __len__(self) -> int:
        return len(self._pending) + len(self._texts)

    def _render(self, entry: _Entry) -> RenderedChart:
        """Emit a chart: numbered in emission order, with the workbook's skip count."""
        self._emitted += 1
        rendered = entry.rendered or self._render_entry(entry)
        rendered.metadata["chart_index"] = self._emitted
        rendered.metadata["workbook_charts_skipped"] = len(self.skipped)
        return rendered

    def _render_entry(self, entry: _Entry, *, deferred: bool = False) -> RenderedChart:
        rendered = render_chart(
            entry.placement,
            entry.chart,
            chart_index=0,
            workbook_filename=self._workbook_filename,
            max_points=self._limits.max_points,
            max_bytes=self._limits.max_batch_bytes,
            unresolved_references=entry.unresolved,
            formatter=self._formatter,
            hidden_sources=entry.hidden_sources,
            hidden_data_withheld=entry.hidden_data_withheld,
            covering_tables=entry.covering_tables,
            max_points_from_tables=self._limits.max_points_from_tables,
            deferred=deferred,
        )
        # Tables the chart plots, by table_id, so a chart can be found with its data.
        rendered.metadata["chart_source_table_ids"] = list(entry.source_tables)
        return rendered

    def before_sheet(self, sheet_index: int) -> Iterator[RenderedChart]:
        """Render, in order, the charts and text boxes of every sheet before ``sheet_index``."""
        return self._drain(sheet_index)

    def remaining(self) -> Iterator[RenderedChart]:
        return self._drain(None)

    def _drain(self, limit: int | None) -> Iterator[RenderedChart]:
        """Yield sheet by sheet: a sheet's charts, then its text boxes."""
        while True:
            chart_sheet = self._pending[0].placement.sheet_index if self._pending else None
            text_sheet = self._texts[0][0] if self._texts else None
            ready = [index for index in (chart_sheet, text_sheet) if index is not None]
            ready = [index for index in ready if limit is None or index < limit]
            if not ready:
                return
            if chart_sheet == min(ready):
                yield self._render(self._pending.pop(0))
            else:
                yield self._texts.pop(0)[1]

    def _text_boxes(
        self,
        inventory,
        resolve: Resolver,
        hidden_sheets: frozenset[str],
        hidden_cells: HiddenCells,
        *,
        withhold: bool,
    ) -> list[tuple[int, RenderedChart]]:
        """Render each sheet's text boxes; ``without_hidden_data`` leaves out hidden
        ones and those linked to a hidden cell."""
        by_sheet: dict[int, list] = {}
        for box in inventory.text_boxes:
            if withhold and _shape_withheld(box.shape, box.sheet_name, resolve, hidden_sheets, hidden_cells):
                continue
            by_sheet.setdefault(box.sheet_index, []).append(box)
        texts = []
        for sheet_index, boxes in by_sheet.items():
            entries = [
                (box.anchor, ("(hidden in Excel) " if box.shape.hidden else "") + box.shape.text) for box in boxes
            ]
            for rendered in render_text_boxes(
                entries,
                sheet_name=boxes[0].sheet_name,
                sheet_index=sheet_index,
                sheet_kind=boxes[0].sheet_kind,
                workbook_filename=self._workbook_filename,
                max_bytes=self._limits.max_batch_bytes,
            ):
                texts.append((sheet_index, rendered))
        return texts


def _shape_withheld(
    shape: ShapeText, sheet: str, resolve: Resolver, hidden_sheets: frozenset[str], hidden_cells: HiddenCells
) -> bool:
    if shape.hidden:
        return True
    cell_range = resolve(_qualified(shape.textlink, sheet)) if shape.textlink else None
    if cell_range is None:
        return bool(shape.textlink)
    if cell_range.sheet in hidden_sheets:
        return True
    rows, columns = hidden_cells(cell_range.sheet)
    return _intersects(cell_range, frozenset(rows), frozenset(columns))
