"""Render a chart as searchable text and chunk metadata.

The text states what a reader of the chart sees, then its data::

    Chart: Monthly revenue
    Type: clustered column chart
    Worksheet: Sales, placed over E2:L20
    Axes: Month (categories); EUR (values)
    Data: categories 'Sales'!$A$2:$A$4; 2025 = 'Sales'!$B$2:$B$4; 2026 = 'Sales'!$C$2:$C$4

    | Month | 2025 | 2026 |
    | --- | --- | --- |
    | Jan | 100 | 200 |

Series sharing the same categories (or x values) are one table, one column per
series, even across the plots of a combination chart (columns for actuals, a
line for targets), so a question such as "revenue in March 2026" or "months
below target" finds the category and every value on one line. Values are the stored numbers, like worksheet tables;
dates are the exception, since a date axis caches Excel serial numbers
(``45658``) that mean nothing to a reader, so they are written as ISO dates.
Percentages and currencies keep their raw value and get a ``Display:`` line,
as tables do. The point count is capped; the text says when points were left
out.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

from core.utils.consts import XLSX_TEXT_NUMBER_FORMAT_KINDS
from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, from_excel

from ..base import markdown_row
from .charts import Chart, DataRef, Plot, Series
from .package import ChartPlacement

_MAX_DATA_LINE = 2000
# Series per data table: a wider row would not fit one chunk, so more series
# sharing the same categories become several tables, each with the categories.
_MAX_SERIES_PER_TABLE = 8
# Series described in chunk metadata, which every chunk of the chart carries.
_MAX_SERIES_METADATA = 64
# Chunk metadata is stored in a 64 KiB JSON field and repeated on every chunk
# of a chart. Strings and lists are cut here, predictably and with a flag
# (``chart_metadata_truncated``), so the store never has to cut them itself;
# the chunk text keeps what a reader needs.
_MAX_METADATA_TEXT = 128
_MAX_METADATA_TITLE = 512
_MAX_METADATA_LIST = 64


class _Capped:
    """Cut metadata strings and lists, remembering whether anything was cut."""

    def __init__(self) -> None:
        self.truncated = False

    def text(self, value: str | None, limit: int = _MAX_METADATA_TEXT) -> str | None:
        if value is None or len(value) <= limit:
            return value
        self.truncated = True
        return value[: limit - 1] + "…"

    def items(self, values: list, limit: int = _MAX_METADATA_LIST) -> list:
        if len(values) <= limit:
            return values
        self.truncated = True
        return values[:limit]


@dataclass(frozen=True)
class RenderedChart:
    text: str
    metadata: dict
    # A chart rendered before its cells' header labels and number formats
    # were read: the plots behind each table's category column, by position,
    # to name those columns once the cells are known (:func:`complete_chart`).
    pending_headers: tuple[tuple[int, ...], ...] | None = None


# Placeholders of a chart rendered before its cell lookups. XML 1.0 forbids
# NUL, so no cached or cell text can contain one.
_HEADER_SLOT = "\x00header:{}\x00"
_DISPLAY_SLOT = "\x00display\x00"
_DISPLAY_RESERVE = _MAX_DATA_LINE + 64


def _number(text: str) -> str:
    """Drop binary noise from cached numbers (``2.9999999999999996`` -> ``3``)."""
    try:
        value = float(text)
    except ValueError:
        return text
    if value != value or value in {float("inf"), float("-inf")}:
        return text
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return format(value, ".15g")


@dataclass(frozen=True)
class ValueFormatter:
    """Turn cached chart values into the text a reader expects."""

    date1904: bool = False
    # Describes a safe display format for (value, format code), the way table
    # cells are described; returns None for anything it does not interpret.
    describe_format: Callable[[Any, str], dict | None] = field(default=lambda value, code: None)

    def _date(self, text: str, reference: DataRef) -> str | None:
        if not reference.format_code or not is_date_format(reference.format_code):
            return None
        try:
            serial = float(text)
            value = from_excel(serial, CALENDAR_MAC_1904 if self.date1904 else CALENDAR_WINDOWS_1900)
        except (ValueError, OverflowError, TypeError):
            return None
        if isinstance(value, datetime) and value.time() == time():
            return value.date().isoformat()
        return value.isoformat()

    def cell(self, text: str, reference: DataRef | None) -> str:
        if reference is None or not reference.numeric or not text:
            return _single_line(text)
        return self._date(text, reference) or _number(text)

    def display(self, reference: DataRef | None) -> dict | None:
        """Describe how a numeric series is displayed: percentages and currencies."""
        if reference is None or not reference.numeric or not reference.format_code:
            return None
        sample = next((value for value in reference.values or () if value), None)
        try:
            described = self.describe_format(float(sample), reference.format_code) if sample else None
        except ValueError:
            return None
        if not described or described.get("kind") not in XLSX_TEXT_NUMBER_FORMAT_KINDS:
            return None
        return {**described, "example_raw": _number(sample)}


def _single_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _values(reference: DataRef | None) -> tuple[str, ...]:
    if reference is None or reference.withheld:
        return ()
    return reference.values or ()


def _series_label(series: Series, number: int) -> str:
    return _single_line(series.name) or f"Series {number}"


#: A series with its plot and its number in the chart (``Series 3``).
_Member = tuple[Plot, int, Series]


def _numbered(chart: Chart) -> list[_Member]:
    """Every series of the chart, numbered across plots so unnamed ones stay distinct."""
    return [
        (plot, number, series)
        for number, (plot, series) in enumerate(((plot, series) for plot in chart.plots for series in plot.series), 1)
    ]


def _groups(chart: Chart) -> list[tuple[tuple[str, ...], list[_Member]]]:
    """Group series that share their categories (or x values), in chart order.

    Grouping crosses plots: a combination chart's columns and line over the
    same months are one table. Category and x-value series never share one,
    nor do bubble series, whose rows carry sizes. Groups are found by hashing
    the categories, so many series cost one pass, not one comparison per pair.
    """
    groups: dict[tuple[bool, bool, tuple[str, ...]], list[_Member]] = {}
    for plot, number, series in _numbered(chart):
        if series.values is not None and series.values.withheld:
            continue
        key = (plot.xy, plot.kind == "bubbleChart", _values(series.categories))
        groups.setdefault(key, []).append((plot, number, series))
    return [
        (categories, members[start : start + _MAX_SERIES_PER_TABLE])
        for (_, _, categories), members in groups.items()
        for start in range(0, len(members), _MAX_SERIES_PER_TABLE)
    ]


def _first_header(plots: list[Plot]) -> str:
    """The category column's header: an axis title, else the header cell."""
    for plot in plots:
        title = _single_line(plot.category_axis_title) or _single_line(plot.category_header)
        if title:
            return title
    return "X" if plots[0].xy else "Category"


def _table(
    categories, members: list[_Member], budget: int, byte_budget: int, formatter: ValueFormatter, first: str
) -> tuple[list[str], int, int, int]:
    """Render one table within a point and byte budget.

    Return its lines, the points shown, the points in total and the bytes used.
    """
    xy = members[0][0].xy
    length = max([len(categories)] + [len(_values(series.values)) for _, _, series in members])
    # Points of a range only partly read count in the total, not in the rows.
    declared = max(
        [length]
        + [series.values.point_count for _, _, series in members if series.values and not series.values.withheld]
    )
    headers = [first]
    # A column of data labels only for a series whose labels say something.
    labelled = {id(series) for _, _, series in members if any(_values(series.labels))}
    for plot, number, series in members:
        label = _series_label(series, number)
        headers.append(label)
        if plot.kind == "bubbleChart":
            headers.append(f"{label} (size)")
        if id(series) in labelled:
            headers.append(f"{label} (data label)")
    lines = [markdown_row(headers), markdown_row(["---"] * len(headers))]
    used = sum(len(line.encode("utf-8")) + 1 for line in lines)
    shown = 0
    total = declared * len(members)
    if used > byte_budget:
        return [], 0, total, 0
    for index in range(length):
        if shown + len(members) > budget:
            break
        label = categories[index] if index < len(categories) else ""
        # Every member shares these categories; the first one's format applies.
        label = formatter.cell(label, members[0][2].categories)
        cells = [label if xy else label or str(index + 1)]
        for plot, _, series in members:
            values = _values(series.values)
            cells.append(formatter.cell(values[index], series.values) if index < len(values) else "")
            if plot.kind == "bubbleChart":
                sizes = _values(series.sizes)
                cells.append(formatter.cell(sizes[index], series.sizes) if index < len(sizes) else "")
            if id(series) in labelled:
                labels = _values(series.labels)
                cells.append(_single_line(labels[index]) if index < len(labels) else "")
        line = markdown_row(cells)
        size = len(line.encode("utf-8")) + 1
        if used + size > byte_budget:
            break
        lines.append(line)
        used += size
        shown += len(members)
    return lines, shown, total, used


def _data_line(plot: Plot, members: list[_Member]) -> str | None:
    parts = []
    for _, number, series in (member for member in members if member[0] is plot):
        label = _series_label(series, number)
        if series.categories is not None and series.categories.formula:
            axis = "x" if plot.xy else "categories"
            part = f"{axis} {series.categories.formula}"
            if part not in parts:
                parts.append(part)
        if series.values is not None and series.values.formula:
            parts.append(f"{label} = {series.values.formula}")
    line = "; ".join(parts)
    # Hundreds of series would crowd out the data itself; the metadata keeps all.
    return (line[: _MAX_DATA_LINE - 2] + " …" if len(line) > _MAX_DATA_LINE else line) or None


_MAX_SUMMARY_SERIES = 20


def _summary_lines(chart: Chart, formatter: ValueFormatter) -> list[str]:
    """Describe every point of each numeric series once its table was cut short.

    The first points alone can misrepresent a long series (a year of daily
    values cut at March); the extremes and the total cover all of them.
    """
    lines = []
    for _, number, series in _numbered(chart):
        if len(lines) == _MAX_SUMMARY_SERIES:
            return lines
        values = _values(series.values)
        categories = _values(series.categories)
        numbers = []
        for index, text in enumerate(values):
            try:
                numbers.append((float(text), index))
            except ValueError:
                continue
        numbers = [(value, index) for value, index in numbers if value == value]
        if not numbers or series.values is None or not series.values.numeric:
            continue

        def at(index: int) -> str:
            if index >= len(categories):
                return f"point {index + 1}"
            return formatter.cell(categories[index], series.categories) or f"point {index + 1}"

        low = min(numbers)
        high = max(numbers)
        total = sum(value for value, _ in numbers)
        declared = series.values.point_count
        scope = f"first {len(values)} of {declared} points" if declared > len(values) else f"all {len(values)} points"
        lines.append(
            f"{_series_label(series, number)}, {scope}: "
            f"min {_number(repr(low[0]))} ({at(low[1])}), max {_number(repr(high[0]))} ({at(high[1])}), "
            f"total {_number(repr(total))}"
        )
    return lines


def _display_line(chart: Chart, formatter: ValueFormatter) -> str | None:
    parts = []
    for _, number, series in _numbered(chart):
        described = formatter.display(series.values)
        if described:
            parts.append(
                f"{_series_label(series, number)} shown as {described['kind']} "
                f"({described['example_raw']} -> {described['display_example']})"
            )
    return "; ".join(parts)[:_MAX_DATA_LINE] or None


def _series_metadata(chart: Chart, formatter: ValueFormatter, capped: _Capped) -> list[dict]:
    entries = []
    numbered = _numbered(chart)
    capped.items(numbered, _MAX_SERIES_METADATA)
    for _, number, series in numbered[:_MAX_SERIES_METADATA]:
        entries.append(
            {
                "name": capped.text(_series_label(series, number)),
                "name_ref": capped.text(series.name_ref),
                "categories_ref": capped.text(series.categories.formula if series.categories is not None else None),
                "values_ref": capped.text(series.values.formula if series.values is not None else None),
                "point_count": series.values.point_count if series.values and not series.values.withheld else 0,
                "values_available": series.values is not None
                and series.values.values is not None
                and not series.values.withheld,
                "values_withheld": series.values is not None and series.values.withheld,
                "format_code": capped.text(series.values.format_code if series.values is not None else None),
                "display_format": formatter.display(series.values),
            }
        )
    return entries


def _chart_context(title: str, labels: list[str], placement: ChartPlacement) -> str:
    """One line naming a chart, repeated on every piece of a split chart.

    The type and the sheet come first and the title last, so a line cut to
    fit (a long title, a small chunk size) loses part of the title, not what
    says where the chart is.
    """
    kind = " + ".join(dict.fromkeys(labels)) or "chart"
    where = "chart sheet" if placement.sheet_kind == "chartsheet" else "worksheet"
    return f"Chart ({kind}), {where} {_single_line(placement.sheet_name)}: {title}"[:_MAX_DATA_LINE]


def render_chart(
    placement: ChartPlacement,
    chart: Chart,
    *,
    chart_index: int,
    workbook_filename: str,
    max_points: int,
    max_bytes: int,
    unresolved_references: int = 0,
    ranges_not_read: int = 0,
    formatter: ValueFormatter | None = None,
    hidden_sources: tuple[str, ...] = (),
    hidden_data_withheld: bool = False,
    covering_tables: tuple[str, ...] = (),
    max_points_from_tables: int | None = None,
    deferred: bool = False,
) -> RenderedChart:
    """Render a chart; with ``deferred``, leave its category headers and display
    line as placeholders that :func:`complete_chart` fills."""
    formatter = formatter or ValueFormatter()
    title = _single_line(chart.title) or _single_line(placement.drawing_name) or f"Chart {placement.position}"
    sheet = _single_line(placement.sheet_name)
    lines = [f"Chart: {title}"]
    labels = [plot.label for plot in chart.plots]
    if labels:
        lines.append("Type: " + " + ".join(dict.fromkeys(labels)))
    if placement.sheet_kind == "chartsheet":
        lines.append(f"Chart sheet: {sheet}")
    else:
        lines.append(f"Worksheet: {sheet}" + (f", placed over {placement.anchor}" if placement.anchor else ""))
    if placement.hidden:
        lines.append("Hidden in Excel.")
    if chart.pivot_table:
        where = f" (worksheet {_single_line(chart.pivot_sheet)})" if chart.pivot_sheet else ""
        lines.append(f"Pivot chart of pivot table {_single_line(chart.pivot_table)}{where}")
    if hidden_sources:
        names = ", ".join(f"'{_single_line(name)}'" for name in hidden_sources)
        plural = "s" if len(hidden_sources) > 1 else ""
        if hidden_data_withheld:
            lines.append(f"Source data: hidden worksheet{plural} {names}; its values are not indexed.")
        else:
            lines.append(f"Source data: hidden worksheet{plural} {names}.")
    axes = []
    for plot in chart.plots:
        category_name = "x" if plot.xy else "categories"
        value_name = "y" if plot.xy else "values"
        for axis_title, name in ((plot.category_axis_title, category_name), (plot.value_axis_title, value_name)):
            entry = f"{_single_line(axis_title)} ({name})" if axis_title else None
            if entry and entry not in axes:
                axes.append(entry)
    if axes:
        lines.append("Axes: " + "; ".join(axes))
    if chart.annotations:
        drawn = "; ".join(f'"{_single_line(shape.text)}"' for shape in chart.annotations)
        lines.append("Text on the chart: " + drawn[:_MAX_DATA_LINE])
    members = _numbered(chart)
    for plot in chart.plots:
        data_line = _data_line(plot, members)
        if data_line:
            lines.append("Data: " + data_line)
    if deferred:
        lines.append(_DISPLAY_SLOT)
    else:
        display_line = _display_line(chart, formatter)
        if display_line:
            lines.append("Display: " + display_line + ". Values below are the raw numbers.")

    budget = max_points
    # The heading and notes stay small; leave them a fixed share of the batch.
    byte_budget = max_bytes - len("\n".join(lines).encode("utf-8")) - 1024 - (_DISPLAY_RESERVE if deferred else 0)
    shown_total = 0
    points_total = 0
    tables = []
    header_plots: list[tuple[int, ...]] = []
    plot_positions = {id(plot): position for position, plot in enumerate(chart.plots)}
    for categories, group in _groups(chart):
        plots = list(dict.fromkeys(id(plot) for plot, _, _ in group))
        first = _first_header([plot for plot, _, _ in group])
        if deferred:
            first = _HEADER_SLOT.format(len(header_plots))
            header_plots.append(tuple(plot_positions[plot] for plot in plots))
        table, shown, total, used = _table(categories, group, budget, byte_budget, formatter, first)
        budget -= shown
        byte_budget -= used
        shown_total += shown
        points_total += total
        if table:
            tables.append("\n".join(table))
    notes = []
    # Data already indexed as worksheet tables is not copied a second time
    # past a small size: the chart keeps its summary and points to the tables.
    data_in_tables = (
        bool(covering_tables) and max_points_from_tables is not None and (points_total > max_points_from_tables)
    )
    if data_in_tables:
        tables = []
        shown_total = 0
        notes.append(
            f"The {points_total} plotted data points are indexed with the worksheet table {', '.join(covering_tables)}."
        )
    elif shown_total < points_total:
        notes.append(f"Only the first {shown_total} of {points_total} data points are shown.")
    if data_in_tables or shown_total < points_total:
        summary = _summary_lines(chart, formatter)
        if summary:
            tables.append("Summary:\n" + "\n".join(f"- {line}" for line in summary))
    for plot in chart.plots:
        if plot.total_points:
            # Named from categories that reach the index, else by position.
            categories = _values(plot.series[0].categories) if plot.series else ()
            names = [
                (formatter.cell(categories[index], plot.series[0].categories) if index < len(categories) else "")
                or f"point {index + 1}"
                for index in plot.total_points
            ]
            notes.append("Totals (bars showing the running total): " + ", ".join(names) + ".")
    trendlines = [
        f"{_series_label(series, number)}: {', '.join(series.trendlines)}"
        for _, number, series in _numbered(chart)
        if series.trendlines
    ]
    if trendlines:
        notes.append(("Trendlines: " + "; ".join(trendlines))[:_MAX_DATA_LINE] + ".")
    notes.extend(chart.notes)
    if unresolved_references:
        notes.append(f"Values of {unresolved_references} series could not be read from the workbook.")
    if ranges_not_read:
        notes.append(
            f"{ranges_not_read} of this chart's cell ranges were not read: the workbook's chart read budget "
            "was reached."
        )
    if chart.unsupported_plots:
        notes.append("Part of this chart uses a chart type that is not extracted.")
    if not chart.series:
        notes.append("This chart has no data series.")

    text = "\n".join(lines)
    if tables:
        text += "\n\n" + "\n\n".join(tables)
    if notes:
        text += "\n\n" + " ".join(notes)
    if not deferred:
        text = _bounded(text, max_bytes)
    sheet_ref = "'" + placement.sheet_name.replace("'", "''") + "'!"
    capped = _Capped()
    series_entries = _series_metadata(chart, formatter, capped)
    metadata = {
        "tabular_format": "xlsx",
        "xlsx_content_kind": "chart",
        "workbook_filename": workbook_filename,
        "sheet_name": placement.sheet_name,
        "sheet_index": placement.sheet_index,
        "sheet_kind": placement.sheet_kind,
        "chart_id": f"sheet:{placement.sheet_index}:chart:{placement.position}",
        "chart_index": chart_index,
        "chart_title": capped.text(chart.title, _MAX_METADATA_TITLE),
        # Repeated at the top of every chunk of a chart split across chunks.
        "chart_context": _chart_context(title, labels, placement),
        "chart_types": list(dict.fromkeys(labels)),
        "chart_format": placement.format,
        "chart_pivot_table": capped.text(chart.pivot_table),
        "chart_pivot_sheet": capped.text(chart.pivot_sheet),
        "chart_anchor_range": f"{sheet_ref}{placement.anchor}" if placement.anchor else None,
        "chart_series": series_entries,
        "chart_series_count": len(chart.series),
        "chart_data_in_tables": data_in_tables,
        "chart_point_count": points_total,
        "chart_points_truncated": shown_total < points_total and not data_in_tables,
        "chart_unresolved_series": unresolved_references,
        "chart_ranges_not_read": ranges_not_read,
        "chart_hidden_source_sheets": capped.items(list(hidden_sources)),
        "chart_hidden_data_withheld": hidden_data_withheld,
        "chart_hidden_in_excel": placement.hidden,
        "spreadsheet_backend": "ooxml",
    }
    metadata["chart_metadata_truncated"] = capped.truncated
    return RenderedChart(text, metadata, tuple(header_plots) if deferred else None)


def _bounded(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        # Only a crafted title or axis label gets here: keep the batch bounded.
        return encoded[: max_bytes - 4].decode("utf-8", "ignore") + " …"
    return text


def complete_chart(rendered: RenderedChart, chart: Chart, formatter: ValueFormatter, max_bytes: int) -> RenderedChart:
    """Fill the placeholders of a deferred chart from what its cells said.

    ``chart`` needs its plots' headers and its series' number formats, and one
    value per series to describe a display format; the points were rendered.
    """
    text = rendered.text
    for index, positions in enumerate(rendered.pending_headers or ()):
        header = markdown_row([_first_header([chart.plots[position] for position in positions])])
        # A table's first cell, as markdown_row escapes it: "| Month |" -> "Month".
        text = text.replace(_HEADER_SLOT.format(index), header[2:-2], 1)
    display_line = _display_line(chart, formatter)
    display = "Display: " + display_line + ". Values below are the raw numbers." if display_line else None
    text = text.replace(_DISPLAY_SLOT + "\n", display + "\n" if display else "", 1)
    metadata = dict(rendered.metadata)
    entries = [dict(entry) for entry in metadata["chart_series"]]
    capped = _Capped()
    for entry, (_, _, series) in zip(entries, _numbered(chart), strict=False):
        entry["format_code"] = capped.text(series.values.format_code if series.values is not None else None)
        entry["display_format"] = formatter.display(series.values)
    metadata["chart_series"] = entries
    metadata["chart_metadata_truncated"] = metadata.get("chart_metadata_truncated", False) or capped.truncated
    return RenderedChart(_bounded(text, max_bytes), metadata)


def render_text_boxes(
    boxes: list[tuple[str | None, str]],
    *,
    sheet_name: str,
    sheet_index: int,
    sheet_kind: str,
    workbook_filename: str,
    max_bytes: int,
) -> list[RenderedChart]:
    """Render a sheet's text boxes, (anchor, text) in drawing order, as text blocks.

    Each block stays under ``max_bytes``; a sheet with more text starts another.
    """
    where = "chart sheet" if sheet_kind == "chartsheet" else "worksheet"
    heading = f"Text boxes on {where} {_single_line(sheet_name)}:"
    blocks: list[list[tuple[str | None, str]]] = [[]]
    used = len(heading.encode("utf-8")) + 1
    for anchor, text in boxes:
        entry = _text_box_entry(anchor, text)
        size = len(entry.encode("utf-8")) + 1
        if blocks[-1] and used + size > max_bytes:
            blocks.append([])
            used = len(heading.encode("utf-8")) + 1
        blocks[-1].append((anchor, entry))
        used += size
    rendered = []
    for block in blocks:
        if not block:
            continue
        text = _bounded("\n".join([heading, *(entry for _, entry in block)]), max_bytes)
        anchors = [anchor for anchor, _ in block if anchor]
        rendered.append(
            RenderedChart(
                text,
                {
                    "tabular_format": "xlsx",
                    "xlsx_content_kind": "text_box",
                    "workbook_filename": workbook_filename,
                    "sheet_name": sheet_name,
                    "sheet_index": sheet_index,
                    "sheet_kind": sheet_kind,
                    "text_box_count": len(block),
                    "text_box_anchors": anchors[:_MAX_SERIES_METADATA],
                    "spreadsheet_backend": "ooxml",
                },
            )
        )
    return rendered


def _text_box_entry(anchor: str | None, text: str) -> str:
    lines = [_single_line(line) for line in text.splitlines() if line.strip()]
    first = f"- Over {anchor}: " if anchor else "- "
    return first + "\n  ".join(lines)
