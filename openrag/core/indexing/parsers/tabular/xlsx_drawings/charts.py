"""Parse a DrawingML chart part (``xl/charts/chartN.xml``) into a typed model.

Only what a reader of the chart can see is kept: the title, the chart type of
each plot, axis titles, and every series' name, categories and values with the
cell ranges they come from. Styling (colours, markers, layout) is dropped.

Values come from the cache the writing application stored next to each
reference (``c:strCache`` / ``c:numCache``): that is what the chart displayed
when the file was saved. Excel, LibreOffice and XlsxWriter write it; OpenPyXL
does not, so a series may carry a reference without values, which
``references.py`` resolves from the worksheet cells. Literal data
(``c:strLit`` / ``c:numLit``) has no reference and is used as is.

Element names are matched on their local name (``{*}``), so transitional and
strict OOXML namespaces, and writers that use a default namespace, all parse.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from xml.etree.ElementTree import Element

from .shapes import ShapeText

# Plot elements of ``c:plotArea`` and the readable name of each.
_PLOT_NAMES = {
    "barChart": "bar",
    "bar3DChart": "3-D bar",
    "lineChart": "line",
    "line3DChart": "3-D line",
    "pieChart": "pie",
    "pie3DChart": "3-D pie",
    "doughnutChart": "doughnut",
    "ofPieChart": "pie-of-pie",
    "areaChart": "area",
    "area3DChart": "3-D area",
    "scatterChart": "scatter",
    "bubbleChart": "bubble",
    "radarChart": "radar",
    "stockChart": "stock",
    "surfaceChart": "surface",
    "surface3DChart": "3-D surface",
}
# Plots whose series give x/y values instead of categories/values.
XY_PLOTS = frozenset({"scatterChart", "bubbleChart"})
_GROUPING_PREFIX = {"stacked": "stacked ", "percentStacked": "100% stacked "}
_AXIS_TAGS = ("catAx", "dateAx", "valAx", "serAx")
# Sparse point indexes are untrusted; expanding them must not create an
# arbitrarily large tuple from a tiny chart part.
_MAX_SPARSE_CACHE_POINTS = 100_000


def _checked_cache_count(points: dict[int, str], declared: str | None) -> int:
    count = int(declared) if declared and declared.isdigit() else (max(points) + 1 if points else 0)
    count = min(count, max(points) + 1 if points else 0)
    if count > max(_MAX_SPARSE_CACHE_POINTS, len(points) * 4):
        raise ValueError("chart cache has excessive sparse point indexes")
    return count


@dataclass(frozen=True)
class DataRef:
    """One series dimension: the cell range it reads and the values it shows.

    ``values`` is ``None`` when the file stores no cache for ``formula``; an
    empty tuple means the range is genuinely empty.
    """

    formula: str | None
    values: tuple[str, ...] | None
    format_code: str | None = None
    # Numbers (c:numRef / c:numLit) are rendered as numbers, or as dates when
    # their format code is a date format; text keeps its exact spelling.
    numeric: bool = False
    # Left out on purpose: the values come from a hidden source and the
    # ``without_hidden_data`` chart mode keeps them out of the index.
    withheld: bool = False
    # Cells in the source range when only its first ``len(values)`` were read
    # (a range larger than ``max_chart_points``); None when all were read.
    declared_points: int | None = None

    @property
    def point_count(self) -> int:
        return max(len(self.values or ()), self.declared_points or 0)

    @property
    def needs_cells(self) -> bool:
        return self.values is None and bool(self.formula) and not self.withheld


@dataclass(frozen=True)
class Series:
    name: str | None
    name_ref: str | None
    categories: DataRef | None
    values: DataRef | None
    # Bubble charts only: the third dimension.
    sizes: DataRef | None = None
    # Data labels that say more than the value: text typed on a point, or
    # labels taken from a cell range (Excel 2013+). One entry per point, by
    # point index like the values, "" where a point has none.
    labels: DataRef | None = None
    label_refs: tuple[tuple[int, str], ...] = ()
    # Trendlines drawn over the series: "linear", "moving average (period 3)".
    trendlines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plot:
    kind: str
    label: str
    series: tuple[Series, ...]
    category_axis_title: str | None = None
    value_axis_title: str | None = None
    # Waterfall points drawn as running totals, by position. Kept as positions,
    # not labels, so they are named only from categories that reach the index.
    total_points: tuple[int, ...] = ()
    # Header of the category column when the file names one (Office 2016
    # charts point at the header cell: ``cx:nf``), and that cell's reference.
    category_header: str | None = None
    category_header_ref: str | None = None

    @property
    def xy(self) -> bool:
        return self.kind in XY_PLOTS


@dataclass(frozen=True)
class Chart:
    title: str | None
    plots: tuple[Plot, ...]
    # Excel's default: hidden rows and columns are not plotted.
    plot_visible_only: bool = True
    unsupported_plots: tuple[str, ...] = field(default=())
    # A title taken from a cell, kept when the file stores no cached text.
    title_ref: str | None = None
    # What the chart computes from its values that the values do not show
    # (histogram bins, box plots), stated in the chunk. Never built from data.
    notes: tuple[str, ...] = ()
    # A pivot chart's pivot table and the worksheet it sits on
    # (``c:pivotSource``: ``[Sales.xlsx]Pivot!PivotTable1``).
    pivot_table: str | None = None
    pivot_sheet: str | None = None
    # Text drawn on the chart: its text boxes (``c:userShapes``).
    annotations: tuple[ShapeText, ...] = ()

    @property
    def series(self) -> tuple[Series, ...]:
        return tuple(series for plot in self.plots for series in plot.series)


def _local(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child(element: Element | None, name: str) -> Element | None:
    return None if element is None else element.find(f"{{*}}{name}")


def _val(element: Element | None, name: str) -> str | None:
    child = _child(element, name)
    return None if child is None else child.get("val")


def _rich_text(element: Element | None) -> str | None:
    """Join a ``c:rich`` body: runs within a paragraph, paragraphs by a space."""
    if element is None:
        return None
    paragraphs = []
    for paragraph in element.iterfind(".//{*}p"):
        text = "".join(run.text or "" for run in paragraph.iterfind(".//{*}t"))
        if text.strip():
            paragraphs.append(text.strip())
    return " ".join(paragraphs) or None


def _cache_points(cache: Element) -> tuple[str, ...]:
    """Expand a ``c:pt`` list to ``ptCount`` positions; missing points are empty.

    Children are compared by local name directly: a ``{*}`` path lookup per
    point costs as much as parsing the XML on a chart with many points.
    """
    points: dict[int, str] = {}
    for point in cache:
        if _local(point) != "pt":
            continue
        index = point.get("idx")
        if index is None or not index.isdigit():
            continue
        points[int(index)] = next((child.text or "" for child in point if _local(child) == "v"), "")
    declared = _val(cache, "ptCount")
    count = _checked_cache_count(points, declared)
    return tuple(points.get(index, "") for index in range(count))


def _multi_level_points(cache: Element) -> tuple[str, ...]:
    """Combine hierarchical category labels, outer level first (``2025 / Q1``).

    Each ``c:lvl`` lists labels only where its group starts, innermost level
    first in the file; outer labels are carried forward over their group.
    """
    levels = [_cache_points(level) for level in cache.findall("{*}lvl")]
    declared = _val(cache, "ptCount")
    count = int(declared) if declared and declared.isdigit() else max((len(level) for level in levels), default=0)
    count = min(count, max((len(level) for level in levels), default=0))
    filled = []
    for level in levels:
        current = ""
        column = []
        for index in range(count):
            label = level[index] if index < len(level) else ""
            current = label or current
            column.append(current)
        filled.append(column)
    return tuple(
        " / ".join(label for label in (column[index] for column in reversed(filled)) if label) for index in range(count)
    )


def _data_ref(element: Element | None) -> DataRef | None:
    """Read ``c:cat`` / ``c:val`` / ``c:xVal`` / ``c:yVal`` / ``c:bubbleSize``."""
    if element is None:
        return None
    for source in element:
        name = _local(source)
        formula_node = _child(source, "f")
        formula = (formula_node.text or "").strip() or None if formula_node is not None else None
        if name in {"strRef", "numRef"}:
            numeric = name == "numRef"
            cache = _child(source, "numCache" if numeric else "strCache")
            if cache is None:
                return DataRef(formula, None, numeric=numeric)
            format_node = _child(cache, "formatCode")
            return DataRef(
                formula, _cache_points(cache), format_node.text if format_node is not None else None, numeric=numeric
            )
        if name == "multiLvlStrRef":
            cache = _child(source, "multiLvlStrCache")
            return DataRef(formula, None if cache is None else _multi_level_points(cache))
        if name in {"strLit", "numLit"}:
            format_node = _child(source, "formatCode")
            return DataRef(
                None,
                _cache_points(source),
                format_node.text if format_node is not None else None,
                numeric=name == "numLit",
            )
    return None


def _series_name(series: Element) -> tuple[str | None, str | None]:
    tx = _child(series, "tx")
    if tx is None:
        return None, None
    reference = _child(tx, "strRef")
    if reference is not None:
        formula_node = _child(reference, "f")
        formula = (formula_node.text or "").strip() or None if formula_node is not None else None
        cache = _child(reference, "strCache")
        values = _cache_points(cache) if cache is not None else ()
        name = " ".join(value for value in values if value).strip() or None
        return name, formula
    literal = _child(tx, "v")
    return ((literal.text or "").strip() or None if literal is not None else None), None


_TRENDLINES = {
    "linear": "linear",
    "exp": "exponential",
    "log": "logarithmic",
    "movingAvg": "moving average",
    "poly": "polynomial",
    "power": "power",
}


def _label_text(label: Element) -> tuple[str, str | None]:
    """A point's own label: typed text (``c:rich``) or a cell's cached text."""
    tx = _child(label, "tx")
    rich = _child(tx, "rich")
    if rich is not None:
        runs = ("".join(run.text or "" for run in paragraph.iter() if _local(run) == "t") for paragraph in rich)
        return " ".join(" ".join(runs).split()), None
    reference = _child(tx, "strRef")
    cache = _child(reference, "strCache")
    formula = _child(reference, "f")
    return (
        " ".join(value for value in _cache_points(cache) if value) if cache is not None else "",
        (formula.text or "").strip() or None if formula is not None else None,
    )


def _data_labels(series: Element) -> tuple[DataRef | None, tuple[tuple[int, str], ...]]:
    """Labels that carry text of their own; automatic value labels add nothing.

    Labels from a cell range (Excel 2013+) come with their range and a cache,
    shown when the series asks for them; a point's own label wins over them.
    """
    labels = _child(series, "dLbls")
    if labels is None:
        return None, ()
    points: dict[int, str] = {}
    refs: dict[int, str] = {}
    formula = None
    shown = labels.find(".//{*}showDataLabelsRange")
    source = series.find("{*}extLst/{*}ext/{*}datalabelsRange")
    if source is not None and shown is not None and shown.get("val", "1").lower() in {"1", "true"}:
        formula_node = _child(source, "f")
        formula = (formula_node.text or "").strip() or None if formula_node is not None else None
        cache = _child(source, "dlblRangeCache")
        if cache is not None:
            points.update(enumerate(_cache_points(cache)))
    for label in labels.findall("{*}dLbl"):
        index = _val(label, "idx")
        if index is None or not index.isdigit():
            continue
        point = int(index)
        if _val(label, "delete") in {"1", "true"}:
            points.pop(point, None)
            refs.pop(point, None)
            continue
        text, reference = _label_text(label)
        if text:
            points[point] = text
            refs.pop(point, None)
            if reference:
                refs[point] = reference
    if not any(points.values()):
        return None, ()
    count = _checked_cache_count(points, None)
    # Indexed by point, like the values; the range only says where they come from.
    values = tuple(" ".join(points.get(index, "").split()) for index in range(count))
    return DataRef(formula, values), tuple(refs.items())


def _trendlines(series: Element) -> tuple[str, ...]:
    found = []
    for trendline in series.findall("{*}trendline"):
        kind = _TRENDLINES.get(_val(trendline, "trendlineType") or "")
        if kind is None:
            continue
        if kind == "polynomial" and _val(trendline, "order"):
            kind += f" (order {_val(trendline, 'order')})"
        if kind == "moving average" and _val(trendline, "period"):
            kind += f" (period {_val(trendline, 'period')})"
        name = _child(trendline, "name")
        label = " ".join((name.text or "").split()) if name is not None else ""
        found.append(f"{label} ({kind} trendline)" if label else f"{kind} trendline")
    return tuple(found)


def _series(series: Element, xy: bool) -> Series:
    name, name_ref = _series_name(series)
    labels, label_refs = _data_labels(series)
    extras = {"labels": labels, "label_refs": label_refs, "trendlines": _trendlines(series)}
    if xy:
        return Series(
            name,
            name_ref,
            _data_ref(_child(series, "xVal")),
            _data_ref(_child(series, "yVal")),
            _data_ref(_child(series, "bubbleSize")),
            **extras,
        )
    return Series(name, name_ref, _data_ref(_child(series, "cat")), _data_ref(_child(series, "val")), **extras)


def _plot_label(plot: Element, kind: str) -> str:
    base = _PLOT_NAMES[kind]
    if kind in {"barChart", "bar3DChart"}:
        base = base.replace("bar", "column") if _val(plot, "barDir") == "col" else base
    grouping = _val(plot, "grouping")
    if kind in {"barChart", "bar3DChart"}:
        # Excel's default bar grouping, and the name its chart picker uses.
        base = _GROUPING_PREFIX.get(grouping or "clustered", "clustered ") + base
    elif kind in {"lineChart", "line3DChart", "areaChart", "area3DChart"}:
        base = _GROUPING_PREFIX.get(grouping or "", "") + base
    if kind == "ofPieChart" and _val(plot, "ofPieType") == "bar":
        base = "bar-of-pie"
    return f"{base} chart"


def _axis_titles(plot_area: Element) -> dict[str, tuple[str, str | None]]:
    """Map each axis id to its kind and title."""
    axes = {}
    for tag in _AXIS_TAGS:
        for axis in plot_area.findall(f"{{*}}{tag}"):
            axis_id = _val(axis, "axId")
            if axis_id is None:
                continue
            title = _child(axis, "title")
            text = _rich_text(_child(_child(title, "tx"), "rich")) if title is not None else None
            axes[axis_id] = (tag, text)
    return axes


def _date_axis_format(plot_area: Element, plot: Element) -> str | None:
    """Return a date format for the plot's category axis when it shows dates.

    A category cache may say ``General`` (XlsxWriter writes that) while the
    axis is a date axis or carries a date number format: Excel then shows the
    serial numbers as dates, and so must the index.
    """
    from openpyxl.styles.numbers import is_date_format

    own = {axis.get("val") for axis in plot.findall("{*}axId")}
    for tag in ("dateAx", "catAx"):
        for axis in plot_area.findall(f"{{*}}{tag}"):
            if _val(axis, "axId") not in own:
                continue
            number_format = _child(axis, "numFmt")
            code = number_format.get("formatCode") if number_format is not None else None
            if code and code != "General" and is_date_format(code):
                return code
            if tag == "dateAx":
                return "yyyy-mm-dd"
    return None


def _with_date_categories(series: Series, date_format: str | None) -> Series:
    categories = series.categories
    if date_format is None or categories is None or not categories.numeric:
        return series
    # Only a cache that states no format of its own takes the axis's: an
    # explicit number format (``0``) is what Excel shows, even on a date axis.
    if categories.format_code not in (None, "", "General"):
        return series
    return replace(series, categories=replace(categories, format_code=date_format))


def _plot_axes(plot: Element, axes: dict[str, tuple[str, str | None]], xy: bool) -> tuple[str | None, str | None]:
    """Return (category or x axis title, value or y axis title) for one plot."""
    own = [axes[axis.get("val")] for axis in plot.findall("{*}axId") if axis.get("val") in axes]
    if xy:
        # Scatter and bubble plots use two value axes: x first, then y.
        values = [title for tag, title in own if tag == "valAx"]
        return (values[0] if values else None), (values[1] if len(values) > 1 else None)
    category = next((title for tag, title in own if tag in {"catAx", "dateAx"}), None)
    value = next((title for tag, title in own if tag == "valAx"), None)
    return category, value


def _chart_title(chart: Element) -> tuple[str | None, str | None]:
    """Return the chart title, or the cell reference it comes from when uncached."""
    title = _child(chart, "title")
    if title is None:
        return None, None
    tx = _child(title, "tx")
    rich = _rich_text(_child(tx, "rich"))
    if rich:
        return rich, None
    reference = _child(tx, "strRef")
    formula_node = _child(reference, "f")
    formula = (formula_node.text or "").strip() or None if formula_node is not None else None
    cache = _child(reference, "strCache")
    if cache is not None:
        return " ".join(value for value in _cache_points(cache) if value).strip() or None, formula
    return None, formula


def _pivot_source(root: Element) -> tuple[str | None, str | None]:
    """Return (pivot table, worksheet) of a pivot chart, or (None, None).

    The name reads ``[Workbook.xlsx]Sheet!PivotTable1``; the workbook part and
    any quoting of the sheet are dropped.
    """
    name = _child(_child(root, "pivotSource"), "name")
    text = (name.text or "").strip() if name is not None else ""
    if not text:
        return None, None
    text = text.split("]", 1)[1] if text.startswith("[") and "]" in text else text
    sheet, _, table = text.rpartition("!")
    sheet = sheet[1:-1].replace("''", "'") if sheet.startswith("'") and sheet.endswith("'") else sheet
    return (table or None), (sheet or None)


def parse_chart(root: Element) -> Chart:
    """Build a :class:`Chart` from a parsed ``c:chartSpace`` element."""
    chart = _child(root, "chart")
    if chart is None:
        raise ValueError("chart part has no c:chart element")
    plot_area = _child(chart, "plotArea")
    axes = _axis_titles(plot_area) if plot_area is not None else {}
    plots = []
    unsupported = []
    for element in plot_area if plot_area is not None else ():
        kind = _local(element)
        if not kind.endswith("Chart"):
            continue
        if kind not in _PLOT_NAMES:
            unsupported.append(kind)
            continue
        xy = kind in XY_PLOTS
        category_title, value_title = _plot_axes(element, axes, xy)
        date_format = None if xy else _date_axis_format(plot_area, element)
        plots.append(
            Plot(
                kind,
                _plot_label(element, kind),
                tuple(_with_date_categories(_series(series, xy), date_format) for series in element.findall("{*}ser")),
                category_title,
                value_title,
            )
        )
    plot_visible_only = _val(chart, "plotVisOnly")
    title, title_ref = _chart_title(chart)
    pivot_table, pivot_sheet = _pivot_source(root)
    return Chart(
        title,
        tuple(plots),
        plot_visible_only=plot_visible_only not in {"0", "false"},
        unsupported_plots=tuple(unsupported),
        title_ref=title_ref,
        pivot_table=pivot_table,
        pivot_sheet=pivot_sheet,
    )
