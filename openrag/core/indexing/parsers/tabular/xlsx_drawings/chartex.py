"""Parse an Office 2016 chart part (``xl/charts/chartExN.xml``) into a :class:`Chart`.

Waterfall, treemap, sunburst, funnel, histogram, Pareto, box and whisker and
map charts use the ``cx:`` schema instead of the classic ``c:`` one. Their
data sits in one ``cx:chartData`` block, and each series points at its data
with ``cx:dataId``::

    cx:chartSpace
      cx:chartData
        cx:data id="0"
          cx:strDim type="cat"   cx:f, cx:lvl (one per hierarchy level)
          cx:numDim type="val"   cx:f, cx:lvl ptCount formatCode
      cx:chart
        cx:title
        cx:plotArea
          cx:plotAreaRegion
            cx:series layoutId="waterfall"   cx:tx, cx:dataId, cx:layoutPr
          cx:axis (cx:catScaling | cx:valScaling, cx:title)

The result is the same model as a classic chart, so rendering, chart modes,
hidden-data rules and limits apply unchanged. The chart type comes from the
series ``layoutId``; a histogram or box plot keeps the raw values Excel bins
or summarizes, with a note saying so.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element

from .charts import Chart, DataRef, Plot, Series, _checked_cache_count

_LAYOUTS = {
    "waterfall": "waterfall chart",
    "treemap": "treemap chart",
    "sunburst": "sunburst chart",
    "funnel": "funnel chart",
    "clusteredColumn": "histogram",
    "paretoLine": "Pareto line",
    "boxWhisker": "box and whisker chart",
    "regionMap": "map chart",
}
# Dimension types a series takes its values from, in order of preference:
# treemaps and sunbursts size their boxes with ``size``, map charts colour
# their regions with ``colorVal``.
_VALUE_TYPES = ("val", "size", "y", "colorVal")
_CATEGORY_TYPES = ("cat", "x")


def _local(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _text(element: Element | None) -> str | None:
    """Text of a ``cx:tx`` / ``cx:title``: its cached ``cx:v``, else its runs."""
    if element is None:
        return None
    cached = element.find(".//{*}txData/{*}v")
    if cached is not None and (cached.text or "").strip():
        return cached.text.strip()
    runs = [run.text or "" for run in element.iterfind(".//{*}t")]
    return " ".join("".join(runs).split()) or None


def _formula(element: Element | None) -> str | None:
    node = element.find("{*}f") if element is not None else None
    return (node.text or "").strip() or None if node is not None else None


def _level_points(level: Element) -> tuple[str, ...]:
    points: dict[int, str] = {}
    for point in level.findall("{*}pt"):
        index = point.get("idx")
        if index is not None and index.isdigit():
            points[int(index)] = point.text or ""
    declared = level.get("ptCount")
    count = _checked_cache_count(points, declared)
    return tuple(points.get(index, "") for index in range(count))


def _dimension(element: Element, numeric: bool) -> DataRef:
    """Read a ``cx:strDim`` / ``cx:numDim``; several levels form a hierarchy.

    Levels are listed innermost first, like classic multi-level categories, and
    an outer label is carried over the points of its group: ``Europe / France``.
    """
    levels = [_level_points(level) for level in element.findall("{*}lvl")]
    first = element.find("{*}lvl")
    format_code = first.get("formatCode") if first is not None else None
    if not levels:
        return DataRef(_formula(element), None, format_code, numeric=numeric)
    if len(levels) == 1:
        return DataRef(_formula(element), levels[0], format_code, numeric=numeric)
    count = max(len(level) for level in levels)
    filled = []
    for level in levels:
        current = ""
        column = []
        for index in range(count):
            label = level[index] if index < len(level) else ""
            current = label or current
            column.append(current)
        filled.append(column)
    values = tuple(
        " / ".join(label for label in (column[index] for column in reversed(filled)) if label) for index in range(count)
    )
    return DataRef(_formula(element), values, format_code, numeric=False)


def _data_blocks(root: Element) -> dict[str, dict[str, tuple[DataRef, str | None]]]:
    """Map each ``cx:data`` id to its dimensions by type (``cat``, ``val``...).

    Each dimension comes with its ``cx:nf``: the reference of its header cell,
    which Excel uses as the series name or category column header.
    """
    blocks: dict[str, dict[str, tuple[DataRef, str | None]]] = {}
    for data in root.iterfind("{*}chartData/{*}data"):
        dimensions: dict[str, tuple[DataRef, str | None]] = {}
        for dimension in data:
            name = _local(dimension)
            if name not in {"strDim", "numDim"}:
                continue
            header = dimension.find("{*}nf")
            header_ref = (header.text or "").strip() or None if header is not None else None
            dimensions.setdefault(
                dimension.get("type", ""), (_dimension(dimension, numeric=name == "numDim"), header_ref)
            )
        blocks[data.get("id", "")] = dimensions
    return blocks


def _axis_titles(plot_area: Element) -> tuple[str | None, str | None]:
    category = value = None
    for axis in plot_area.findall("{*}axis"):
        title = _text(axis.find("{*}title"))
        if axis.find("{*}catScaling") is not None:
            category = category or title
        elif axis.find("{*}valScaling") is not None:
            value = value or title
    return category, value


def _total_points(series: Element) -> tuple[int, ...]:
    """Positions of the waterfall points Excel draws as running totals."""
    return tuple(
        int(index.get("val"))
        for index in series.iterfind("{*}layoutPr/{*}subtotals/{*}idx")
        if (index.get("val") or "").isdigit()
    )


def _layout_notes(series: Element, layout: str) -> list[str]:
    """What the chart computes from the values, which the raw values do not show.

    Notes never quote the data: the chart mode may withhold it after parsing.
    """
    properties = series.find("{*}layoutPr")
    notes = []
    if layout == "clusteredColumn":
        binning = properties.find("{*}binning") if properties is not None else None
        detail = ""
        if binning is not None:
            size = binning.find("{*}binSize")
            count = binning.find("{*}binCount")
            if size is not None and size.get("val"):
                detail = f" of width {size.get('val')}"
            elif count is not None and count.get("val"):
                detail = f" ({count.get('val')} bins)"
        notes.append(f"Excel groups these values into bins{detail}; the values below are the raw data.")
    if layout == "boxWhisker":
        notes.append("Excel draws quartiles, median and outliers from these values; the values below are the raw data.")
    return notes


def parse_chartex(root: Element) -> Chart:
    """Build a :class:`Chart` from a parsed ``cx:chartSpace`` element."""
    chart = root.find("{*}chart")
    if chart is None:
        raise ValueError("chartex part has no cx:chart element")
    blocks = _data_blocks(root)
    plot_area = chart.find("{*}plotArea")
    category_title, value_title = _axis_titles(plot_area) if plot_area is not None else (None, None)
    by_layout: dict[str, list[Series]] = {}
    notes: list[str] = []
    totals: set[int] = set()
    header_refs: dict[str, str | None] = {}
    unsupported: list[str] = []
    for element in plot_area.iterfind("{*}plotAreaRegion/{*}series") if plot_area is not None else ():
        layout = element.get("layoutId", "")
        if layout not in _LAYOUTS:
            unsupported.append(layout or "unknown")
            continue
        data_id = element.find("{*}dataId")
        dimensions = blocks.get(data_id.get("val", "") if data_id is not None else "", {})
        categories, category_header_ref = next(
            (dimensions[kind] for kind in _CATEGORY_TYPES if kind in dimensions), (None, None)
        )
        values, values_header_ref = next(
            (dimensions[kind] for kind in _VALUE_TYPES if kind in dimensions), (None, None)
        )
        header_refs.setdefault(layout, category_header_ref)
        if categories is None and values is None:
            # A Pareto line has no data of its own: Excel derives it from the
            # histogram it belongs to.
            if layout == "paretoLine":
                by_layout.setdefault(layout, [])
                continue
        tx = element.find("{*}tx")
        # Without its own name, a series is named by its values' header cell.
        name_ref = _formula(tx.find("{*}txData")) if tx is not None else None
        series = Series(_text(tx), name_ref or (values_header_ref if tx is None else None), categories, values)
        by_layout.setdefault(layout, []).append(series)
        notes.extend(_layout_notes(element, layout))
        if layout == "waterfall":
            totals.update(_total_points(element))
    plots = []
    for layout, members in by_layout.items():
        if not members:
            continue
        label = _LAYOUTS[layout]
        # A histogram with a Pareto line is Excel's Pareto chart.
        if layout == "clusteredColumn" and "paretoLine" in by_layout:
            label = "Pareto chart"
        plots.append(
            Plot(
                f"chartex:{layout}",
                label,
                tuple(members),
                category_title,
                value_title,
                total_points=tuple(sorted(totals)) if layout == "waterfall" else (),
                category_header_ref=header_refs.get(layout),
            )
        )
    return Chart(
        _text(chart.find("{*}title")),
        tuple(plots),
        unsupported_plots=tuple(unsupported),
        title_ref=_formula(chart.find("{*}title/{*}tx/{*}txData")),
        notes=tuple(dict.fromkeys(notes)),
    )
