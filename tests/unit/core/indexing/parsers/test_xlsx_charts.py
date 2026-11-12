"""Charts in XLSX workbooks: location, parsing, cell resolution, rendering, wiring.

Cached charts (what Excel, LibreOffice and XlsxWriter write) are built as raw
chart XML so each case is exact; uncached charts come from OpenPyXL, which
stores only the cell references.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from core.indexing.parsers.tabular.xlsx_drawings.references import (
    CellRange,
    parse_reference,
    resolve_ranges,
    series_values,
)
from core.indexing.parsers.tabular.xlsx_drawings.render import _number
from core.indexing.parsers.tabular.xlsx_parser import XlsxParser
from core.models.document import Document, DocumentType
from core.utils.exceptions import ConfigError, NoIndexableContentError
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference

_NS = (
    'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
)


# --- chart XML builders ------------------------------------------------------


def _points(values) -> str:
    return f'<c:ptCount val="{len(values)}"/>' + "".join(
        f'<c:pt idx="{index}"><c:v>{value}</c:v></c:pt>' for index, value in enumerate(values) if value is not None
    )


def _ref(formula, values, *, numeric) -> str:
    kind = "num" if numeric else "str"
    cache = "" if values is None else f"<c:{kind}Cache>{_points(values)}</c:{kind}Cache>"
    return f"<c:{kind}Ref><c:f>{formula}</c:f>{cache}</c:{kind}Ref>"


def _series(name, categories, values, *, index=0, xy=False, numeric_categories=False, name_ref="Data!$B$1") -> str:
    """``categories`` / ``values`` are (formula, cached values or None)."""
    first, second = ("xVal", "yVal") if xy else ("cat", "val")
    tx = f"<c:tx><c:strRef><c:f>{name_ref}</c:f><c:strCache>{_points([name])}</c:strCache></c:strRef></c:tx>"
    return (
        f'<c:ser><c:idx val="{index}"/><c:order val="{index}"/>{tx if name else ""}'
        f"<c:{first}>{_ref(*categories, numeric=numeric_categories or xy)}</c:{first}>"
        f"<c:{second}>{_ref(*values, numeric=True)}</c:{second}></c:ser>"
    )


def _axis(tag, axis_id, title=None) -> str:
    title_xml = (
        f"<c:title><c:tx><c:rich><a:p><a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx></c:title>" if title else ""
    )
    return f'<c:{tag}><c:axId val="{axis_id}"/>{title_xml}</c:{tag}>'


def _plot(tag, series, *, extra="", axes=(1, 2)) -> str:
    ids = "".join(f'<c:axId val="{axis}"/>' for axis in axes)
    return f"<c:{tag}>{extra}{''.join(series)}{ids}</c:{tag}>"


def _chart_xml(plots, *, title=None, axes="") -> str:
    title_xml = (
        f"<c:title><c:tx><c:rich><a:p><a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx></c:title>" if title else ""
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><c:chartSpace {_NS}><c:chart>{title_xml}'
        f'<c:plotArea>{"".join(plots)}{axes}</c:plotArea><c:plotVisOnly val="1"/></c:chart></c:chartSpace>'
    )


def _monthly_revenue(**kwargs) -> str:
    months = ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"])
    return _chart_xml(
        [
            _plot(
                "barChart",
                [
                    _series("2025", months, ("Data!$B$2:$B$4", [100, 101.5, 2.9999999999999996])),
                    _series("2026", months, ("Data!$C$2:$C$4", [200, 201, 202]), index=1, name_ref="Data!$C$1"),
                ],
                extra='<c:barDir val="col"/><c:grouping val="clustered"/>',
            )
        ],
        title="Monthly revenue",
        axes=_axis("catAx", 1, "Month") + _axis("valAx", 2, "EUR"),
        **kwargs,
    )


# --- workbook builders -------------------------------------------------------


def _data_sheet(workbook, title="Data"):
    sheet = workbook.active
    sheet.title = title
    for row in (["Month", "2025", "2026"], ["Jan", 100, 200], ["Feb", 101.5, 201], ["Mar", 3, 202]):
        sheet.append(row)
    return sheet


def _add_chart(sheet, data_sheet, anchor="E2"):
    chart = BarChart()
    chart.title = "Revenue"
    chart.add_data(Reference(data_sheet, min_col=2, min_row=1, max_col=3, max_row=4), titles_from_data=True)
    chart.set_categories(Reference(data_sheet, min_col=1, min_row=2, max_row=4))
    sheet.add_chart(chart, anchor)


def _save(workbook) -> bytes:
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _replace_parts(raw: bytes, parts: dict[str, bytes | str | None]) -> bytes:
    """Rewrite a package, replacing (or with ``None``, removing) parts."""
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(raw)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as dest:
        for name in source.namelist():
            if name in parts:
                if parts[name] is not None:
                    dest.writestr(name, parts[name])
            else:
                dest.writestr(name, source.read(name))
        for name, data in parts.items():
            if name not in source.namelist() and data is not None:
                dest.writestr(name, data)
    return output.getvalue()


def _workbook_with_chart(chart_xml: str | None = None, *, charts: int = 1) -> bytes:
    """A data sheet with ``charts`` charts; the first one's XML replaced if given."""
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    for index in range(charts):
        _add_chart(sheet, sheet, anchor=f"E{2 + 20 * index}")
    raw = _save(workbook)
    return _replace_parts(raw, {"xl/charts/chart1.xml": chart_xml}) if chart_xml else raw


def _document(raw: bytes) -> Document:
    return Document(filename="charts.xlsx", content_type=DocumentType.XLSX, raw_bytes=raw)


def _blocks(raw: bytes, *, mode: str = "full", **limits):
    return list(XlsxParser(limits=limits or None, charts=mode).iter_batches(_document(raw)))


def _charts(raw: bytes, *, mode: str = "full", **limits):
    charts = [block for block in _blocks(raw, mode=mode, **limits) if block.metadata["xlsx_content_kind"] == "chart"]
    # A chart rendered before its cell lookups never leaks a placeholder.
    assert not any("\x00" in chart.text for chart in charts)
    return charts


# --- rendering of cached charts ---------------------------------------------


def test_cached_chart_is_rendered_as_text_with_one_column_per_series():
    (chart,) = _charts(_workbook_with_chart(_monthly_revenue()))

    assert chart.text.startswith(
        "Chart: Monthly revenue\n"
        "Type: clustered column chart\n"
        "Worksheet: Data, placed over E2\n"
        "Axes: Month (categories); EUR (values)\n"
        "Data: categories Data!$A$2:$A$4; 2025 = Data!$B$2:$B$4; 2026 = Data!$C$2:$C$4\n"
    )
    assert "| Month | 2025 | 2026 |\n| --- | --- | --- |\n| Jan | 100 | 200 |" in chart.text
    # Binary noise in cached numbers is dropped; real decimals are kept.
    assert "| Feb | 101.5 | 201 |" in chart.text
    assert "| Mar | 3 | 202 |" in chart.text
    assert chart.block_type == "text"


def test_chart_metadata_identifies_the_chart_and_its_source_ranges():
    (chart,) = _charts(_workbook_with_chart(_monthly_revenue()))
    metadata = chart.metadata

    assert metadata["xlsx_content_kind"] == "chart"
    assert metadata["chart_id"] == "sheet:0:chart:1"
    assert metadata["chart_index"] == 1
    assert metadata["chart_title"] == "Monthly revenue"
    assert metadata["chart_types"] == ["clustered column chart"]
    assert metadata["chart_anchor_range"] == "'Data'!E2"
    assert metadata["sheet_kind"] == "worksheet"
    assert metadata["chart_point_count"] == 6
    assert metadata["chart_points_truncated"] is False
    assert [series["values_ref"] for series in metadata["chart_series"]] == ["Data!$B$2:$B$4", "Data!$C$2:$C$4"]
    assert all(series["values_available"] for series in metadata["chart_series"])
    assert metadata["workbook_chart_extraction"] == "complete"
    assert metadata["workbook_chart_issues"] == []


def test_two_cell_anchor_gives_the_covered_range():
    """Excel anchors a chart by its top-left and bottom-right cells."""
    import re

    raw = _workbook_with_chart(_monthly_revenue())
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        drawing = archive.read("xl/drawings/drawing1.xml").decode()
    drawing = re.sub(
        r"<(\w+:)?oneCellAnchor>(.*?</(?:\w+:)?from>)<(?:\w+:)?ext [^>]*/>",
        r"<\1twoCellAnchor>\2<\1to><\1col>11</\1col><\1colOff>0</\1colOff><\1row>15</\1row>"
        r"<\1rowOff>0</\1rowOff></\1to>",
        drawing,
    )
    drawing = re.sub(r"</(\w+:)?oneCellAnchor>", r"</\1twoCellAnchor>", drawing)
    (chart,) = _charts(_replace_parts(raw, {"xl/drawings/drawing1.xml": drawing}))

    assert "Worksheet: Data, placed over E2:L16" in chart.text
    assert chart.metadata["chart_anchor_range"] == "'Data'!E2:L16"


def test_chart_follows_the_tables_of_its_sheet_and_precedes_the_next_sheet():
    workbook = Workbook()
    data = _data_sheet(workbook)
    _add_chart(data, data)
    other = workbook.create_sheet("Notes")
    other.append(["key", "value"])
    other.append(["a", 1])
    blocks = _blocks(_save(workbook))

    assert [(block.metadata["sheet_name"], block.metadata["xlsx_content_kind"]) for block in blocks] == [
        ("Data", "table"),
        ("Data", "chart"),
        ("Notes", "table"),
    ]


def test_series_with_different_categories_get_separate_tables():
    xml = _chart_xml(
        [
            _plot(
                "lineChart",
                [
                    _series("North", ("Data!$A$2:$A$3", ["Q1", "Q2"]), ("Data!$B$2:$B$3", [1, 2])),
                    _series("South", ("Data!$D$2:$D$3", ["Jan", "Feb"]), ("Data!$E$2:$E$3", [3, 4]), index=1),
                ],
            )
        ]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| Category | North |\n| --- | --- |\n| Q1 | 1 |\n| Q2 | 2 |" in chart.text
    assert "| Category | South |\n| --- | --- |\n| Jan | 3 |\n| Feb | 4 |" in chart.text


def test_scatter_chart_uses_its_axis_titles_for_x_and_y():
    xml = _chart_xml(
        [
            _plot(
                "scatterChart",
                [_series("Runs", ("Data!$A$2:$A$4", [1, 2.5, 4]), ("Data!$B$2:$B$4", [10, 20, 40]), xy=True)],
            )
        ],
        axes=_axis("valAx", 1, "Speed") + _axis("valAx", 2, "Distance"),
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "Type: scatter chart" in chart.text
    assert "Axes: Speed (x); Distance (y)" in chart.text
    assert "| Speed | Runs |\n| --- | --- |\n| 1 | 10 |\n| 2.5 | 20 |\n| 4 | 40 |" in chart.text


def test_combo_chart_lists_every_plot_type():
    months = ("Data!$A$2:$A$3", ["Jan", "Feb"])
    xml = _chart_xml(
        [
            _plot("barChart", [_series("Sales", months, ("Data!$B$2:$B$3", [5, 6]))], extra='<c:barDir val="col"/>'),
            _plot("lineChart", [_series("Target", months, ("Data!$C$2:$C$3", [7, 8]), index=1)], axes=(1, 3)),
        ]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "Type: clustered column chart + line chart" in chart.text
    assert chart.metadata["chart_types"] == ["clustered column chart", "line chart"]


@pytest.mark.parametrize(
    ("tag", "extra", "label"),
    [
        ("barChart", '<c:barDir val="bar"/><c:grouping val="stacked"/>', "stacked bar chart"),
        ("barChart", '<c:barDir val="col"/><c:grouping val="percentStacked"/>', "100% stacked column chart"),
        ("areaChart", '<c:grouping val="stacked"/>', "stacked area chart"),
        ("pieChart", "", "pie chart"),
        ("doughnutChart", "", "doughnut chart"),
        ("ofPieChart", '<c:ofPieType val="bar"/>', "bar-of-pie chart"),
        ("radarChart", "", "radar chart"),
        ("line3DChart", "", "3-D line chart"),
    ],
)
def test_chart_type_labels(tag, extra, label):
    xml = _chart_xml(
        [_plot(tag, [_series("S", ("Data!$A$2:$A$3", ["a", "b"]), ("Data!$B$2:$B$3", [1, 2]))], extra=extra)]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert f"Type: {label}" in chart.text


def test_multi_level_categories_are_combined_outer_level_first():
    cache = (
        '<c:multiLvlStrCache><c:ptCount val="4"/>'
        '<c:lvl><c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt>'
        '<c:pt idx="2"><c:v>Q1</c:v></c:pt><c:pt idx="3"><c:v>Q2</c:v></c:pt></c:lvl>'
        '<c:lvl><c:pt idx="0"><c:v>2025</c:v></c:pt><c:pt idx="2"><c:v>2026</c:v></c:pt></c:lvl>'
        "</c:multiLvlStrCache>"
    )
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f"<c:cat><c:multiLvlStrRef><c:f>Data!$A$2:$B$5</c:f>{cache}</c:multiLvlStrRef></c:cat>"
        f"<c:val>{_ref('Data!$C$2:$C$5', [1, 2, 3, 4], numeric=True)}</c:val></c:ser>"
    )
    (chart,) = _charts(_workbook_with_chart(_chart_xml([_plot("barChart", [series])])))

    for label, value in (("2025 / Q1", 1), ("2025 / Q2", 2), ("2026 / Q1", 3), ("2026 / Q2", 4)):
        assert f"| {label} | {value} |" in chart.text


def test_literal_data_without_a_reference_is_rendered():
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/><c:tx><c:v>Typed in</c:v></c:tx>'
        f"<c:cat><c:strLit>{_points(['x', 'y'])}</c:strLit></c:cat>"
        f"<c:val><c:numLit>{_points([7, 8])}</c:numLit></c:val></c:ser>"
    )
    (chart,) = _charts(_workbook_with_chart(_chart_xml([_plot("pieChart", [series])])))

    assert "| Category | Typed in |" in chart.text
    assert "| x | 7 |" in chart.text
    assert "Data:" not in chart.text


def test_missing_cached_points_render_as_empty_cells():
    xml = _chart_xml(
        [_plot("lineChart", [_series("S", ("Data!$A$2:$A$4", ["a", "b", "c"]), ("Data!$B$2:$B$4", [1, None, 3]))])]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| b |  |" in chart.text


def test_untitled_chart_falls_back_to_its_drawing_name():
    xml = _chart_xml([_plot("lineChart", [_series("S", ("Data!$A$2:$A$3", ["a", "b"]), ("Data!$B$2:$B$3", [1, 2]))])])
    (chart,) = _charts(_workbook_with_chart(xml))

    assert chart.text.startswith("Chart: Chart 1\n")
    assert chart.metadata["chart_title"] is None


# --- uncached charts (OpenPyXL) ----------------------------------------------


def test_uncached_chart_reads_its_values_and_series_names_from_the_cells():
    (chart,) = _charts(_workbook_with_chart())

    # Series named from the header row: the category column takes its header too.
    assert "| Month | 2025 | 2026 |" in chart.text
    assert "| Jan | 100 | 200 |" in chart.text
    assert "| Mar | 3 | 202 |" in chart.text
    assert chart.metadata["chart_unresolved_series"] == 0


def test_uncached_chart_reads_data_from_another_sheet():
    workbook = Workbook()
    data = _data_sheet(workbook)
    dashboard = workbook.create_sheet("Dashboard")
    _add_chart(dashboard, data)
    charts = _charts(_save(workbook))

    assert charts[0].metadata["sheet_name"] == "Dashboard"
    assert "| Feb | 101.5 | 201 |" in charts[0].text


def test_uncached_series_with_an_unresolvable_reference_is_reported():
    xml = _chart_xml([_plot("lineChart", [_series("S", ("Data!$A$2:$A$3", ["a", "b"]), ("MyNamedRange", None))])])
    (chart,) = _charts(_workbook_with_chart(xml))

    assert chart.metadata["chart_unresolved_series"] == 1
    assert "Values of 1 series could not be read from the workbook." in chart.text


def test_uncached_charts_read_each_referenced_sheet_once(monkeypatch):
    from core.indexing.parsers.tabular.xlsx_drawings import extract

    calls = []
    original = extract.resolve_ranges

    def counting(ranges, read_rows, *args, **kwargs):
        def reader(sheet, *bounds):
            calls.append(sheet)
            return read_rows(sheet, *bounds)

        return original(ranges, reader, *args, **kwargs)

    monkeypatch.setattr(extract, "resolve_ranges", counting)
    charts = _charts(_workbook_with_chart(charts=5))

    assert len(charts) == 5
    assert calls == ["Data"]


# --- sheets, visibility, empty workbooks -------------------------------------


def test_chart_sheet_is_indexed_without_an_anchor():
    workbook = Workbook()
    data = _data_sheet(workbook)
    chart = BarChart()
    chart.add_data(Reference(data, min_col=2, min_row=1, max_row=4), titles_from_data=True)
    workbook.create_chartsheet("Overview").add_chart(chart)
    (block,) = _charts(_save(workbook))

    assert "Chart sheet: Overview" in block.text
    assert block.metadata["sheet_kind"] == "chartsheet"
    assert block.metadata["chart_anchor_range"] is None


def test_charts_on_hidden_sheets_are_skipped():
    workbook = Workbook()
    data = _data_sheet(workbook)
    hidden = workbook.create_sheet("Hidden")
    hidden.sheet_state = "hidden"
    _add_chart(hidden, data)

    assert _charts(_save(workbook)) == []


def test_workbook_whose_only_content_is_a_chart_is_indexed():
    workbook = Workbook()
    data = _data_sheet(workbook)
    data.sheet_state = "hidden"
    chart = BarChart()
    chart.add_data(Reference(data, min_col=2, min_row=1, max_row=4), titles_from_data=True)
    workbook.create_chartsheet("Overview").add_chart(chart)
    blocks = _blocks(_save(workbook))

    assert [block.metadata["xlsx_content_kind"] for block in blocks] == ["chart"]


def test_workbook_without_content_or_charts_is_still_rejected():
    workbook = Workbook()
    with pytest.raises(NoIndexableContentError):
        _blocks(_save(workbook))


# --- limits and damaged parts ------------------------------------------------


def test_point_limit_truncates_the_table_and_says_so():
    values = list(range(10))
    xml = _chart_xml(
        [
            _plot(
                "lineChart",
                [_series("S", ("Data!$A$2:$A$11", [f"p{i}" for i in values]), ("Data!$B$2:$B$11", values))],
            )
        ]
    )
    (chart,) = _charts(_workbook_with_chart(xml), max_chart_points=4)

    assert "| p3 | 3 |" in chart.text
    assert "| p4 |" not in chart.text
    assert "Only the first 4 of 10 data points are shown." in chart.text
    assert chart.metadata["chart_points_truncated"] is True
    # The summary covers every point, including those the table left out.
    assert "- S, all 10 points: min 0 (p0), max 9 (p9), total 45" in chart.text


def test_long_labels_cannot_make_a_chart_exceed_the_batch_byte_limit():
    labels = ["x" * 5000 for _ in range(50)]
    xml = _chart_xml(
        [_plot("lineChart", [_series("S", ("Data!$A$2:$A$51", labels), ("Data!$B$2:$B$51", list(range(50))))])]
    )
    (chart,) = _charts(_workbook_with_chart(xml), max_batch_bytes=40_000)

    assert len(chart.text.encode("utf-8")) <= 40_000
    assert chart.metadata["chart_points_truncated"] is True


def test_invalid_chart_xml_is_skipped_and_the_tables_are_still_indexed():
    blocks = _blocks(_workbook_with_chart("<c:chartSpace><unclosed>"))

    assert [block.metadata["xlsx_content_kind"] for block in blocks] == ["table"]


def test_chart_xml_with_an_entity_declaration_is_skipped():
    xml = '<?xml version="1.0"?><!DOCTYPE c [<!ENTITY e "boom">]><c:chartSpace ' + _NS + "><c:chart/></c:chartSpace>"

    assert _charts(_workbook_with_chart(xml)) == []


def test_skipped_charts_are_counted_on_the_others():
    raw = _workbook_with_chart(charts=2)
    raw = _replace_parts(raw, {"xl/charts/chart1.xml": "not xml"})
    (chart,) = _charts(raw)

    assert chart.metadata["workbook_chart_issue_count"] == 1


def test_oversized_chart_part_is_skipped():
    xml = _monthly_revenue().replace("</c:chartSpace>", "<!--" + "x" * 5000 + "--></c:chartSpace>")

    assert _charts(_workbook_with_chart(xml), max_chart_part_bytes=4000) == []


def test_chart_limit_keeps_the_first_charts():
    charts = _charts(_workbook_with_chart(charts=4), max_charts=2)

    assert [chart.metadata["chart_id"] for chart in charts] == ["sheet:0:chart:1", "sheet:0:chart:2"]


# --- chunking ----------------------------------------------------------------


@pytest.mark.parametrize("chunker_name", ["recursive_splitter", "structured_section"])
def test_chart_blocks_are_chunked_as_text(chunker_name):
    from core.chunking.recursive import RecursiveSplitter
    from core.chunking.structured_section import StructuredSectionChunker
    from core.models.document import ProcessedDocument

    def words(text):
        return len(text.split())

    chunker = (
        RecursiveSplitter(chunk_size=400, length_function=words)
        if chunker_name == "recursive_splitter"
        else StructuredSectionChunker(chunk_size=400, min_tokens=0, max_tokens=800, length_function=words)
    )
    (chart,) = _charts(_workbook_with_chart(_monthly_revenue()))
    chunks = chunker.chunk(ProcessedDocument(document_id="c", text_blocks=[chart], metadata=dict(chart.metadata)))

    text = "\n".join(chunk.text for chunk in chunks)
    assert "Chart: Monthly revenue" in text
    assert "| Jan | 100 | 200 |" in text


# --- references and numbers --------------------------------------------------


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("Data!$B$2:$B$4", CellRange("Data", 2, 2, 4, 2)),
        ("'My Sheet'!A1", CellRange("My Sheet", 1, 1, 1, 1)),
        ("'O''Brien'!$C$9:$A$1", CellRange("O'Brien", 1, 1, 9, 3)),
        ("(Data!$A$1,Data!$A$3)", None),
        ("[1]Data!$A$1", None),
        ("MyNamedRange", None),
        ("Data!A1:B", None),
        (None, None),
    ],
)
def test_parse_reference(formula, expected):
    assert parse_reference(formula) == expected


def test_resolve_ranges_reads_vertical_horizontal_and_two_dimensional_ranges():
    grid = {
        1: ("2025", "Q1", 10),
        2: ("", "Q2", 20),
        3: ("2026", "Q1", 30),
    }

    def read_rows(sheet, min_row, max_row, min_column, max_column):
        assert sheet == "S"
        for row in range(min_row, max_row + 1):
            yield grid[row][min_column - 1 : max_column]

    vertical = CellRange("S", 1, 3, 3, 3)
    horizontal = CellRange("S", 1, 1, 1, 3)
    labels = CellRange("S", 1, 1, 3, 2)
    resolved = resolve_ranges(
        [vertical, horizontal, labels], read_rows, lambda value: "" if value is None else str(value), max_cells=100
    )

    assert series_values(vertical, resolved[vertical]) == ("10", "20", "30")
    assert series_values(horizontal, resolved[horizontal]) == ("2025", "Q1", "10")
    # A blank outer label belongs to the group above it.
    assert series_values(labels, resolved[labels]) == ("2025 / Q1", "2025 / Q2", "2026 / Q1")


def test_resolve_ranges_skips_unknown_sheets():
    def read_rows(sheet, *_):
        raise KeyError(sheet)

    assert resolve_ranges([CellRange("Nope", 1, 1, 1, 1)], read_rows, str, max_cells=10) == {}


def test_distant_chart_references_use_bounded_windows():
    from core.indexing.parsers.tabular.xlsx_drawings.references import first_cell_formats

    first = CellRange("S", 1, 1, 1, 1)
    distant = CellRange("S", 1_000_000, 16_000, 1_000_000, 16_000)
    calls = []

    def read_rows(sheet, min_row, max_row, min_column, max_column, **kwargs):
        calls.append((min_row, max_row, min_column, max_column))
        assert (max_row - min_row + 1) * (max_column - min_column + 1) <= 100_000
        return iter([("0.00" if kwargs.get("formats") else "value",)])

    result = resolve_ranges([first, distant], read_rows, str, max_cells=5_000)
    assert result[first] == result[distant] == [["value"]]
    assert len(calls) == 2
    calls.clear()
    assert first_cell_formats([first, distant], read_rows) == {first: "0.00", distant: "0.00"}
    assert len(calls) == 2


@pytest.mark.parametrize("modern", [False, True])
def test_sparse_chart_cache_rejects_amplified_point_indexes(modern):
    from xml.etree.ElementTree import fromstring

    from core.indexing.parsers.tabular.xlsx_drawings.chartex import _level_points
    from core.indexing.parsers.tabular.xlsx_drawings.charts import _cache_points

    element = fromstring('<cache><pt idx="999999"><v>1</v></pt></cache>')
    with pytest.raises(ValueError, match="sparse point indexes"):
        (_level_points if modern else _cache_points)(element)


def test_chart_reference_with_both_dimensions_over_budget_is_clamped():
    requested = CellRange("S", 1, 1, 1_000, 1_000)
    calls = []

    def read_rows(sheet, min_row, max_row, min_column, max_column):
        calls.append((max_row - min_row + 1) * (max_column - min_column + 1))
        return iter(())

    resolve_ranges([requested], read_rows, str, max_cells=10)
    assert calls == [10]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100", "100"),
        ("101.5", "101.5"),
        ("2.9999999999999996", "3"),
        ("0.1", "0.1"),
        ("1E-7", "1e-07"),
        ("abc", "abc"),
    ],
)
def test_cached_numbers_are_normalized(raw, expected):
    assert _number(raw) == expected


# --- dates and display formats -----------------------------------------------


def _num_cache_ref(formula, values, format_code) -> str:
    return (
        f"<c:numRef><c:f>{formula}</c:f><c:numCache><c:formatCode>{format_code}</c:formatCode>"
        f"{_points(values)}</c:numCache></c:numRef>"
    )


def _dated_chart(format_code="mmm-yy", values_format="General", values=(10, 20)) -> str:
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f"<c:cat>{_num_cache_ref('Data!$A$2:$A$3', [45658, 45689], format_code)}</c:cat>"
        f"<c:val>{_num_cache_ref('Data!$B$2:$B$3', list(values), values_format)}</c:val></c:ser>"
    )
    return _chart_xml([_plot("lineChart", [series])], axes=_axis("dateAx", 1) + _axis("valAx", 2))


def test_date_categories_are_written_as_iso_dates():
    (chart,) = _charts(_workbook_with_chart(_dated_chart()))

    assert "| 2025-01-01 | 10 |" in chart.text
    assert "| 2025-02-01 | 20 |" in chart.text
    assert "45658" not in chart.text


def test_date_categories_follow_the_1904_date_system():
    raw = _workbook_with_chart(_dated_chart())
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        workbook = archive.read("xl/workbook.xml").decode()
    workbook = workbook.replace("<workbookPr/>", '<workbookPr date1904="1"/>')
    if 'date1904="1"' not in workbook:
        workbook = workbook.replace("<sheets>", '<workbookPr date1904="1"/><sheets>', 1)
    (chart,) = _charts(_replace_parts(raw, {"xl/workbook.xml": workbook}))

    # The same serial counts from 1904-01-01: four years and a day later.
    assert "| 2029-01-02 | 10 |" in chart.text


def test_date_time_categories_keep_their_time():
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f"<c:cat>{_num_cache_ref('Data!$A$2:$A$2', [45658.5], 'yyyy-mm-dd hh:mm')}</c:cat>"
        f"<c:val>{_num_cache_ref('Data!$B$2:$B$2', [1], 'General')}</c:val></c:ser>"
    )
    (chart,) = _charts(_workbook_with_chart(_chart_xml([_plot("lineChart", [series])])))

    assert "| 2025-01-01T12:00:00 | 1 |" in chart.text


def test_numbers_with_a_non_date_format_stay_numbers():
    (chart,) = _charts(_workbook_with_chart(_dated_chart(format_code="0")))

    assert "| 45658 | 10 |" in chart.text


def test_text_categories_that_look_numeric_keep_their_spelling():
    xml = _chart_xml(
        [_plot("barChart", [_series("S", ("Data!$A$2:$A$3", ["007", "1.50"]), ("Data!$B$2:$B$3", [1, 2]))])]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| 007 | 1 |" in chart.text
    assert "| 1.50 | 2 |" in chart.text


def test_percentage_series_keeps_raw_values_and_states_the_display():
    (chart,) = _charts(_workbook_with_chart(_dated_chart(values_format="0.00%", values=(0.08125, 0.1))))

    assert "Display: Series 1 shown as percentage (0.08125 -> 8.13%). Values below are the raw numbers." in chart.text
    assert "| 2025-01-01 | 0.08125 |" in chart.text
    assert chart.metadata["chart_series"][0]["display_format"]["kind"] == "percentage"


def test_currency_series_states_the_display():
    (chart,) = _charts(_workbook_with_chart(_dated_chart(values_format="#,##0.00 [$€-40C]", values=(1234.5, 10))))

    assert "shown as currency (1234.5 -> 1,234.50 €)" in chart.text


def test_plain_number_formats_add_no_display_line():
    (chart,) = _charts(_workbook_with_chart(_dated_chart(values_format="#,##0")))

    assert "Display:" not in chart.text
    assert chart.metadata["chart_series"][0]["display_format"] is None


def test_uncached_date_categories_are_read_as_dates():
    from datetime import date

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["Day", "Visits"])
    sheet.append([date(2025, 3, 1), 5])
    sheet.append([date(2025, 3, 2), 7])
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(sheet, min_col=1, min_row=2, max_row=3))
    sheet.add_chart(chart, "D2")
    (block,) = _charts(_save(workbook))

    assert "| 2025-03-01 | 5 |" in block.text


def test_hierarchy_labels_carry_over_their_group_as_excel_writes_them():
    """The layout of LibreOffice's Excel-made treemap.xlsx / sunburst.xlsx (B4:D14)."""
    cell_range = CellRange("S", 4, 2, 9, 4)
    grid = [
        ["Best", "First", "A"],
        ["", "", "B"],
        ["", "Second", "C"],
        ["Worst", "Third", "E"],
        ["", "Fourth", ""],  # a two-level branch: no leaf below Fourth
        ["", "", "F"],  # still under Worst / Fourth? No: a leaf with blank parents stays under them
    ]

    assert series_values(cell_range, grid) == (
        "Best / First / A",
        "Best / First / B",
        "Best / Second / C",
        "Worst / Third / E",
        "Worst / Fourth",
        "Worst / Fourth / F",
    )


def test_a_new_outer_label_resets_the_levels_below_it():
    cell_range = CellRange("S", 1, 1, 3, 3)
    grid = [["Europe", "France", "Paris"], ["Asia", "", "Tokyo"], ["", "Japan", "Osaka"]]

    # Tokyo's country is blank under a new continent: it does not inherit France.
    assert series_values(cell_range, grid) == ("Europe / France / Paris", "Asia / Tokyo", "Asia / Japan / Osaka")


def test_series_values_drop_hidden_rows_and_columns():
    cell_range = CellRange("S", 2, 1, 4, 2)
    grid = [["2025", "Q1"], ["", "Q2"], ["2026", "Q1"]]

    assert series_values(cell_range, grid, hidden_rows=frozenset({3})) == ("2025 / Q1", "2026 / Q1")
    assert series_values(cell_range, grid, hidden_columns=frozenset({1})) == ("Q1", "Q2", "Q1")


# --- hidden source cells -----------------------------------------------------


def _chart_over_hidden_row(*, plot_visible_only: bool, filtered: bool = False) -> bytes:
    workbook = Workbook()
    data = _data_sheet(workbook)
    data.row_dimensions[3].hidden = True  # Feb
    if filtered:
        data.auto_filter.ref = "A1:C4"
        data.sheet_properties.filterMode = True
    _add_chart(data, data)
    raw = _save(workbook)
    if not plot_visible_only:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            chart = archive.read("xl/charts/chart1.xml").decode()
        chart = chart.replace('<plotVisOnly val="1"/>', '<plotVisOnly val="0"/>')
        assert 'plotVisOnly val="0"' in chart
        raw = _replace_parts(raw, {"xl/charts/chart1.xml": chart})
    return raw


def test_uncached_chart_leaves_out_hidden_rows_like_excel():
    (chart,) = _charts(_chart_over_hidden_row(plot_visible_only=True))

    assert "| Jan | 100 | 200 |" in chart.text
    assert "Feb" not in chart.text
    assert "| Mar | 3 | 202 |" in chart.text


def test_uncached_chart_leaves_out_rows_hidden_by_a_filter():
    """The table indexes filtered rows; the chart, like Excel's, does not plot them."""
    (chart,) = _charts(_chart_over_hidden_row(plot_visible_only=True, filtered=True))

    assert "Feb" not in chart.text


def test_uncached_chart_set_to_plot_hidden_cells_keeps_them():
    (chart,) = _charts(_chart_over_hidden_row(plot_visible_only=False))

    assert "| Feb | 101.5 | 201 |" in chart.text


# --- link to source tables ---------------------------------------------------


def test_chart_records_the_tables_it_plots():
    workbook = Workbook()
    data = _data_sheet(workbook)
    for row in range(1, 5):
        data.cell(row=row, column=8, value=f"other {row}")
        data.cell(row=row, column=9, value=row)
    dashboard = workbook.create_sheet("Dashboard")
    _add_chart(dashboard, data)
    blocks = _blocks(_save(workbook))
    tables = [block.metadata["table_id"] for block in blocks if block.metadata["xlsx_content_kind"] == "table"]
    (chart,) = [block for block in blocks if block.metadata["xlsx_content_kind"] == "chart"]

    assert len(tables) == 2
    # The chart reads A:C, the first table only.
    assert chart.metadata["chart_source_table_ids"] == ["sheet:0:table:0"]


def test_chart_over_data_on_a_hidden_sheet_links_no_table():
    workbook = Workbook()
    data = _data_sheet(workbook)
    data.sheet_state = "hidden"
    visible = workbook.create_sheet("Report")
    visible.append(["x"])
    visible.append([1])
    _add_chart(visible, data)
    (chart,) = _charts(_save(workbook))

    assert chart.metadata["chart_source_table_ids"] == []
    # Excel draws a chart from a hidden sheet's data, so its values are indexed.
    assert "| Jan | 100 | 200 |" in chart.text


# --- titles from cells and defined names -------------------------------------


def test_uncached_title_taken_from_a_cell_is_read():
    xml = _chart_xml(
        [_plot("lineChart", [_series("S", ("Data!$A$2:$A$3", ["a", "b"]), ("Data!$B$2:$B$3", [1, 2]))])]
    ).replace("<c:chart>", "<c:chart><c:title><c:tx><c:strRef><c:f>Data!$A$1</c:f></c:strRef></c:tx></c:title>", 1)
    (chart,) = _charts(_workbook_with_chart(xml))

    assert chart.text.startswith("Chart: Month\n")
    assert chart.metadata["chart_title"] == "Month"


def _with_defined_names(raw: bytes, names: str) -> bytes:
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        workbook = archive.read("xl/workbook.xml").decode()
    workbook = workbook.replace("</sheets>", f"</sheets><definedNames>{names}</definedNames>", 1)
    return _replace_parts(raw, {"xl/workbook.xml": workbook})


def _named_series_chart(values_formula: str) -> str:
    return _chart_xml(
        [_plot("lineChart", [_series("S", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), (values_formula, None))])]
    )


@pytest.mark.parametrize("formula", ["Sales2025", "Data!Sales2025", "[0]!Sales2025", "sales2025"])
def test_uncached_series_over_a_defined_name_is_resolved(formula):
    raw = _workbook_with_chart(_named_series_chart(formula))
    raw = _with_defined_names(raw, '<definedName name="Sales2025">Data!$B$2:$B$4</definedName>')
    (chart,) = _charts(raw)

    assert "| Jan | 100 |" in chart.text
    assert "| Mar | 3 |" in chart.text
    assert chart.metadata["chart_unresolved_series"] == 0
    assert chart.metadata["chart_source_table_ids"] == ["sheet:0:table:0"]


def test_sheet_scoped_name_wins_over_the_workbook_name():
    raw = _workbook_with_chart(_named_series_chart("Data!Sales"))
    raw = _with_defined_names(
        raw,
        '<definedName name="Sales">Data!$C$2:$C$4</definedName>'
        '<definedName name="Sales" localSheetId="0">Data!$B$2:$B$4</definedName>',
    )
    (chart,) = _charts(raw)

    assert "| Jan | 100 |" in chart.text


def test_defined_name_that_is_not_a_single_range_stays_unresolved():
    raw = _workbook_with_chart(_named_series_chart("Sales"))
    raw = _with_defined_names(raw, '<definedName name="Sales">OFFSET(Data!$B$2,0,0,3,1)</definedName>')
    (chart,) = _charts(raw)

    assert chart.metadata["chart_unresolved_series"] == 1


def test_charts_shown_in_full_have_no_summary():
    (chart,) = _charts(_workbook_with_chart(_monthly_revenue()))

    assert "Summary:" not in chart.text


# --- chart modes: full, without_hidden_data, off ----------------------------


def _chart_over_hidden_sheet(*, cached: bool = False) -> bytes:
    """A visible report sheet whose chart plots data on a hidden sheet."""
    workbook = Workbook()
    data = _data_sheet(workbook)
    data.sheet_state = "hidden"
    report = workbook.create_sheet("Report")
    report.append(["note"])
    report.append(["see chart"])
    _add_chart(report, data)
    raw = _save(workbook)
    return _replace_parts(raw, {"xl/charts/chart1.xml": _monthly_revenue()}) if cached else raw


@pytest.mark.parametrize("cached", [False, True])
def test_full_mode_indexes_hidden_source_data_and_says_where_it_comes_from(cached):
    (chart,) = _charts(_chart_over_hidden_sheet(cached=cached))

    assert "Source data: hidden worksheet 'Data'." in chart.text
    assert "| Jan | 100 | 200 |" in chart.text
    assert chart.metadata["chart_hidden_source_sheets"] == ["Data"]
    assert chart.metadata["chart_hidden_data_withheld"] is False


@pytest.mark.parametrize("cached", [False, True])
def test_without_hidden_data_mode_keeps_the_chart_but_not_its_hidden_values(cached):
    (chart,) = _charts(_chart_over_hidden_sheet(cached=cached), mode="without_hidden_data")

    assert "Source data: hidden worksheet 'Data'; its values are not indexed." in chart.text
    # What the chart itself says stays: its title, type and where its data lives.
    assert chart.text.startswith("Chart: ")
    assert "column chart" in chart.text
    assert "!$B$2:$B$4" in chart.text
    # No value, category or series name read from the hidden sheet.
    for hidden in ("Jan", "Feb", "| 100", "101.5", "2025", "2026"):
        assert hidden not in chart.text, hidden
    assert "|" not in chart.text
    assert chart.metadata["chart_hidden_data_withheld"] is True
    assert all(series["values_withheld"] for series in chart.metadata["chart_series"])
    assert all(series["point_count"] == 0 for series in chart.metadata["chart_series"])


def test_without_hidden_data_mode_never_reads_the_hidden_sheet(monkeypatch):
    from core.indexing.parsers.tabular.xlsx_drawings import extract

    sheets_read = []
    original = extract.resolve_ranges

    def recording(ranges, read_rows, *args, **kwargs):
        sheets_read.extend(cell_range.sheet for cell_range in ranges)
        return original(ranges, read_rows, *args, **kwargs)

    monkeypatch.setattr(extract, "resolve_ranges", recording)
    _charts(_chart_over_hidden_sheet(), mode="without_hidden_data")

    assert "Data" not in sheets_read


def test_without_hidden_data_mode_leaves_charts_over_visible_data_unchanged():
    raw = _workbook_with_chart(_monthly_revenue())

    assert _charts(raw, mode="without_hidden_data")[0].text == _charts(raw)[0].text


def test_without_hidden_data_mode_drops_hidden_rows_even_when_the_chart_plots_them():
    (chart,) = _charts(_chart_over_hidden_row(plot_visible_only=False), mode="without_hidden_data")

    assert "Feb" not in chart.text
    assert "| Jan | 100 | 200 |" in chart.text


def test_cached_points_on_hidden_rows_are_dropped_when_excel_would_not_draw_them():
    """A cache holding one point per cell still maps points to rows."""
    raw = _chart_over_hidden_row(plot_visible_only=True)
    raw = _replace_parts(raw, {"xl/charts/chart1.xml": _monthly_revenue()})
    (chart,) = _charts(raw)

    assert "Feb" not in chart.text
    assert "| Mar | 3 | 202 |" in chart.text


def test_off_mode_reads_no_chart(monkeypatch):
    from core.indexing.parsers.tabular.xlsx_drawings import extract

    monkeypatch.setattr(extract, "parse_chart", lambda *a, **k: pytest.fail("charts must not be read"))
    monkeypatch.setattr(extract, "parse_chartex", lambda *a, **k: pytest.fail("charts must not be read"))
    blocks = _blocks(_workbook_with_chart(_monthly_revenue()), mode="off")

    assert [block.metadata["xlsx_content_kind"] for block in blocks] == ["table"]


def test_off_mode_rejects_a_workbook_whose_only_content_is_charts():
    workbook = Workbook()
    data = _data_sheet(workbook)
    data.sheet_state = "hidden"
    chart = BarChart()
    chart.add_data(Reference(data, min_col=2, min_row=1, max_row=4), titles_from_data=True)
    workbook.create_chartsheet("Overview").add_chart(chart)

    with pytest.raises(NoIndexableContentError):
        _blocks(_save(workbook), mode="off")


def test_unknown_chart_mode_is_a_configuration_error():
    with pytest.raises(ConfigError, match="full, without_hidden_data, off"):
        XlsxParser(charts="hidden")


# --- chunking of large charts ------------------------------------------------


def _long_chart(points: int = 300) -> str:
    categories = (f"Data!$A$2:$A${points + 1}", [f"day-{index}" for index in range(points)])
    return _chart_xml(
        [
            _plot(
                "lineChart",
                [
                    _series("Revenue", categories, (f"Data!$B$2:$B${points + 1}", list(range(points)))),
                    _series("Cost", categories, (f"Data!$C$2:$C${points + 1}", list(range(points))), index=1),
                ],
            )
        ],
        title="Daily revenue",
    )


@pytest.mark.parametrize("chunker_name", ["recursive_splitter", "structured_section"])
def test_every_chunk_of_a_split_chart_names_its_chart_and_repeats_the_header(chunker_name):
    from core.chunking.recursive import RecursiveSplitter
    from core.chunking.structured_section import StructuredSectionChunker
    from core.models.document import ProcessedDocument

    def words(text):
        return len(text.split())

    chunker = (
        RecursiveSplitter(chunk_size=200, length_function=words)
        if chunker_name == "recursive_splitter"
        else StructuredSectionChunker(chunk_size=200, min_tokens=0, max_tokens=400, length_function=words)
    )
    (chart,) = _charts(_workbook_with_chart(_long_chart()))
    chunks = chunker.chunk(ProcessedDocument(document_id="c", text_blocks=[chart], metadata=dict(chart.metadata)))
    bodies = [chunk.content or chunk.text for chunk in chunks]
    rows = [line for body in bodies for line in body.splitlines() if line.startswith("| day-")]

    assert len(chunks) > 3
    # The first piece opens with the chart heading, the others with the
    # one-line context: type and worksheet first, then the title.
    assert bodies[0].startswith("Chart: Daily revenue")
    assert all(body.startswith("Chart (line chart), worksheet Data: Daily revenue") for body in bodies[1:])
    assert all("| Category | Revenue | Cost |" in body for body in bodies if "| day-" in body)
    # Each data row is indexed exactly once, and no chunk exceeds the budget.
    assert len(rows) == len(set(rows)) == 300
    assert all(words(body) <= 200 for body in bodies)
    assert all(chunk.metadata["chart_id"] == "sheet:0:chart:1" for chunk in chunks)


def test_small_chart_stays_one_chunk_with_its_full_heading():
    from core.chunking.markdown_utils import chunk_xlsx_chart

    (chart,) = _charts(_workbook_with_chart(_monthly_revenue()))

    assert chunk_xlsx_chart(chart.text, chart.metadata, 10_000, lambda text: len(text.split())) == [chart.text]


# --- large uncached ranges, data already in tables, wide charts --------------


def _uncached_long_workbook(rows: int, chart_rows: int | None = None) -> bytes:
    from openpyxl.chart import LineChart

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["day", "visits"])
    for index in range(rows):
        sheet.append([f"d{index}", index])
    chart_rows = chart_rows or rows
    chart = LineChart()
    chart.title = "Visits"
    chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=chart_rows + 1), titles_from_data=True)
    chart.set_categories(Reference(sheet, min_col=1, min_row=2, max_row=chart_rows + 1))
    sheet.add_chart(chart, "D2")
    return _save(workbook)


def test_uncached_range_over_the_point_limit_is_read_in_part_not_dropped():
    (chart,) = _charts(_uncached_long_workbook(80), max_chart_points=50, max_chart_points_from_tables=10_000)

    assert chart.metadata["chart_unresolved_series"] == 0
    assert chart.metadata["chart_point_count"] == 80
    assert "| d49 | 49 |" in chart.text
    assert "| d50 |" not in chart.text
    assert "Only the first 50 of 80 data points are shown." in chart.text
    # The summary says it covers only what was read.
    assert "- visits, first 50 of 80 points: min 0 (d0), max 49 (d49), total 1225" in chart.text


def test_large_chart_over_an_indexed_table_points_to_it_instead_of_copying_it():
    blocks = _blocks(_uncached_long_workbook(300))
    (chart,) = [block for block in blocks if block.metadata["xlsx_content_kind"] == "chart"]

    assert "| d0 |" not in chart.text
    assert "- visits, all 300 points: min 0 (d0), max 299 (d299), total 44850" in chart.text
    assert "The 300 plotted data points are indexed with the worksheet table 'Data'!A1:B301." in chart.text
    assert chart.metadata["chart_data_in_tables"] is True
    assert chart.metadata["chart_points_truncated"] is False
    assert chart.metadata["chart_source_table_ids"] == ["sheet:0:table:0"]


def test_small_chart_over_an_indexed_table_keeps_its_data():
    (chart,) = _charts(_uncached_long_workbook(12))

    assert "| d11 | 11 |" in chart.text
    assert chart.metadata["chart_data_in_tables"] is False


def test_chart_reading_beyond_its_table_keeps_its_data():
    """A range that is only partly inside a table is not "already indexed"."""
    xml = _chart_xml(
        [_plot("lineChart", [_series("S", ("Data!$A$2:$A$400", ["x"] * 399), ("Data!$B$2:$B$400", [1] * 399))])]
    )
    (chart,) = _charts(_workbook_with_chart(xml), max_chart_points_from_tables=10)

    assert chart.metadata["chart_data_in_tables"] is False
    assert "| x | 1 |" in chart.text


def test_chart_over_a_hidden_sheet_is_never_treated_as_already_indexed():
    (chart,) = _charts(_chart_over_hidden_sheet(), max_chart_points_from_tables=1)

    assert chart.metadata["chart_data_in_tables"] is False
    assert "| Jan | 100 | 200 |" in chart.text


def test_wide_chart_is_split_into_tables_of_at_most_eight_series():
    categories = ("Data!$A$2:$A$3", ["Q1", "Q2"])
    series = [
        _series(f"Store {index}", categories, ("Data!$B$2:$B$3", [index, index]), index=index, name_ref="Data!$B$1")
        for index in range(20)
    ]
    (chart,) = _charts(_workbook_with_chart(_chart_xml([_plot("barChart", series)])))

    headers = [line for line in chart.text.splitlines() if line.startswith("| ") and "Store" in line]
    assert len(headers) == 3
    assert all(header.count("Store") <= 8 for header in headers)
    assert headers[-1].endswith("| Store 16 | Store 17 | Store 18 | Store 19 |")


def test_series_metadata_is_capped_and_counted():
    categories = ("Data!$A$2:$A$3", ["Q1", "Q2"])
    series = [_series(f"S{index}", categories, ("Data!$B$2:$B$3", [1, 2]), index=index) for index in range(100)]
    (chart,) = _charts(_workbook_with_chart(_chart_xml([_plot("lineChart", series)])))

    assert len(chart.metadata["chart_series"]) == 64
    assert chart.metadata["chart_series_count"] == 100


def test_resolve_ranges_reads_the_first_cells_of_a_range_over_the_limit():
    def read_rows(sheet, min_row, max_row, min_column, max_column):
        for row in range(min_row, max_row + 1):
            yield (row,)

    long_range = CellRange("S", 1, 1, 100, 1)
    (grid,) = resolve_ranges([long_range], read_rows, str, max_cells=10).values()

    assert [cells[0] for cells in grid] == [str(row) for row in range(1, 11)]


# --- Office 2016 charts (cx: chartex) ----------------------------------------

_CX = (
    'xmlns:cx="http://schemas.microsoft.com/office/drawing/2014/chartex" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def _cx_level(values, *, format_code=None) -> str:
    attributes = f' formatCode="{format_code}"' if format_code else ""
    points = "".join(f'<cx:pt idx="{index}">{value}</cx:pt>' for index, value in enumerate(values) if value is not None)
    return f'<cx:lvl ptCount="{len(values)}"{attributes}>{points}</cx:lvl>'


def _cx_data(data_id, *, categories=None, values=None, value_type="val", levels=None, formula="Data!$B$2:$B$6"):
    category_xml = ""
    if levels is not None:
        category_xml = f'<cx:strDim type="cat"><cx:f>Data!$A$2:$C$6</cx:f>{"".join(_cx_level(level) for level in levels)}</cx:strDim>'
    elif categories is not None:
        category_xml = f'<cx:strDim type="cat"><cx:f>Data!$A$2:$A$6</cx:f>{_cx_level(categories)}</cx:strDim>'
    value_xml = (
        f'<cx:numDim type="{value_type}"><cx:f>{formula}</cx:f>{_cx_level(values, format_code="General")}</cx:numDim>'
        if values is not None
        else ""
    )
    return f'<cx:data id="{data_id}">{category_xml}{value_xml}</cx:data>'


def _cx_series(layout, data_id=0, *, name="Amount", layout_properties="") -> str:
    tx = f"<cx:tx><cx:txData><cx:f>Data!$B$1</cx:f><cx:v>{name}</cx:v></cx:txData></cx:tx>" if name else ""
    data = f'<cx:dataId val="{data_id}"/>' if data_id is not None else ""
    properties = f"<cx:layoutPr>{layout_properties}</cx:layoutPr>" if layout_properties else ""
    return f'<cx:series layoutId="{layout}">{tx}{data}{properties}</cx:series>'


def _cx_chart(data, series, *, title="Cash flow", axes="") -> str:
    title_xml = f"<cx:title><cx:tx><cx:txData><cx:v>{title}</cx:v></cx:txData></cx:tx></cx:title>" if title else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><cx:chartSpace {_CX}><cx:chartData>{"".join(data)}</cx:chartData>'
        f"<cx:chart>{title_xml}<cx:plotArea><cx:plotAreaRegion>{''.join(series)}</cx:plotAreaRegion>{axes}"
        "</cx:plotArea></cx:chart></cx:chartSpace>"
    )


def _cx_axis(axis_id, scaling, title=None) -> str:
    title_xml = f"<cx:title><cx:tx><cx:txData><cx:v>{title}</cx:v></cx:txData></cx:tx></cx:title>" if title else ""
    return f'<cx:axis id="{axis_id}"><cx:{scaling}/>{title_xml}</cx:axis>'


def _workbook_with_chartex(
    chartex_xml: str, *, alternate_content: bool = True, hidden_sheet: bool = False, base: bytes | None = None
) -> bytes:
    """Turn the classic chart frame into an Office 2016 one, as Excel writes it.

    With ``hidden_sheet``, the workbook also has a hidden sheet named Secret.
    """
    import re

    if base is not None:
        raw = base
    elif hidden_sheet:
        workbook = Workbook()
        data = _data_sheet(workbook)
        secret = workbook.create_sheet("Secret")
        secret.append(["x"])
        secret.sheet_state = "hidden"
        _add_chart(data, data)
        raw = _save(workbook)
    else:
        raw = _workbook_with_chart()
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        drawing = archive.read("xl/drawings/drawing1.xml").decode()
        rels = archive.read("xl/drawings/_rels/drawing1.xml.rels").decode()
        types = archive.read("[Content_Types].xml").decode()
    drawing = drawing.replace(
        "http://schemas.openxmlformats.org/drawingml/2006/chart",
        "http://schemas.microsoft.com/office/drawing/2014/chartex",
    )
    if alternate_content:
        mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
        drawing = re.sub(
            r"(<(?:\w+:)?graphicFrame\b.*?</(?:\w+:)?graphicFrame>)",
            rf'<mc:AlternateContent {mc}><mc:Choice Requires="cx1">\1</mc:Choice>'
            r"<mc:Fallback><sp><txBody><p><r><t>This chart isn\'t available</t></r></p></txBody></sp></mc:Fallback>"
            r"</mc:AlternateContent>",
            drawing,
            count=1,
            flags=re.DOTALL,
        )
    rels = rels.replace(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart",
        "http://schemas.microsoft.com/office/2014/relationships/chartEx",
    ).replace("/xl/charts/chart1.xml", "/xl/charts/chartEx1.xml")
    types = types.replace(
        "</Types>",
        '<Override PartName="/xl/charts/chartEx1.xml" ContentType="application/vnd.ms-office.chartex+xml"/></Types>',
    )
    return _replace_parts(
        raw,
        {
            "xl/drawings/drawing1.xml": drawing,
            "xl/drawings/_rels/drawing1.xml.rels": rels,
            "[Content_Types].xml": types,
            "xl/charts/chart1.xml": None,
            "xl/charts/chartEx1.xml": chartex_xml,
        },
    )


def _waterfall() -> str:
    return _cx_chart(
        [_cx_data(0, categories=["Start", "Sales", "Costs", "Tax", "End"], values=[100, 50, -30, -5, 115])],
        [_cx_series("waterfall", layout_properties='<cx:subtotals><cx:idx val="0"/><cx:idx val="4"/></cx:subtotals>')],
        axes=_cx_axis(0, "catScaling", "Step") + _cx_axis(1, "valScaling", "EUR"),
    )


@pytest.mark.parametrize("alternate_content", [True, False])
def test_waterfall_chart_is_indexed_with_its_data_and_totals(alternate_content):
    (chart,) = _charts(_workbook_with_chartex(_waterfall(), alternate_content=alternate_content))

    assert chart.text.startswith("Chart: Cash flow\nType: waterfall chart\nWorksheet: Data, placed over E2\n")
    assert "Axes: Step (categories); EUR (values)" in chart.text
    assert "Data: categories Data!$A$2:$A$6; Amount = Data!$B$2:$B$6" in chart.text
    assert "| Step | Amount |\n| --- | --- |\n| Start | 100 |" in chart.text
    assert "| Costs | -30 |" in chart.text
    assert "Totals (bars showing the running total): Start, End." in chart.text
    assert chart.metadata["chart_format"] == "chartex"
    assert chart.metadata["chart_types"] == ["waterfall chart"]


def test_treemap_hierarchy_is_combined_outer_level_first_and_sized_by_its_values():
    xml = _cx_chart(
        [
            _cx_data(
                0,
                levels=[
                    ["Paris", "Lyon", "Berlin", "Munich"],  # leaf level first, as Excel writes it
                    ["France", None, "Germany", None],
                    ["Europe", None, None, None],
                ],
                values=[10, 5, 8, 4],
                value_type="size",
            )
        ],
        [_cx_series("treemap", name="Population")],
        title="Cities",
    )
    (chart,) = _charts(_workbook_with_chartex(xml))

    assert "Type: treemap chart" in chart.text
    for label, value in (
        ("Europe / France / Paris", 10),
        ("Europe / France / Lyon", 5),
        ("Europe / Germany / Munich", 4),
    ):
        assert f"| {label} | {value} |" in chart.text


@pytest.mark.parametrize(
    ("layout", "label"),
    [("sunburst", "sunburst chart"), ("funnel", "funnel chart"), ("regionMap", "map chart")],
)
def test_other_office_2016_chart_types(layout, label):
    xml = _cx_chart([_cx_data(0, categories=["a", "b"], values=[3, 4])], [_cx_series(layout)])
    (chart,) = _charts(_workbook_with_chartex(xml))

    assert f"Type: {label}" in chart.text
    assert "| a | 3 |" in chart.text


def test_histogram_keeps_raw_values_and_states_the_binning():
    xml = _cx_chart(
        [_cx_data(0, values=[12, 15, 21, 22, 37])],
        [
            _cx_series(
                "clusteredColumn", name="Scores", layout_properties='<cx:binning><cx:binSize val="10"/></cx:binning>'
            )
        ],
        title="Score distribution",
    )
    (chart,) = _charts(_workbook_with_chartex(xml))

    assert "Type: histogram" in chart.text
    assert "| 1 | 12 |" in chart.text
    assert "Excel groups these values into bins of width 10; the values below are the raw data." in chart.text


def test_histogram_with_a_pareto_line_is_a_pareto_chart():
    xml = _cx_chart(
        [_cx_data(0, categories=["Late", "Damaged", "Lost"], values=[40, 25, 5])],
        [_cx_series("clusteredColumn", name="Complaints"), _cx_series("paretoLine", data_id=None, name=None)],
    )
    (chart,) = _charts(_workbook_with_chartex(xml))

    assert "Type: Pareto chart" in chart.text
    assert "| Late | 40 |" in chart.text


def test_box_and_whisker_chart_states_what_excel_computes():
    xml = _cx_chart([_cx_data(0, values=[1, 2, 3, 10])], [_cx_series("boxWhisker", name="Delays")])
    (chart,) = _charts(_workbook_with_chartex(xml))

    assert "Type: box and whisker chart" in chart.text
    assert "Excel draws quartiles, median and outliers from these values" in chart.text


def test_unknown_office_2016_layout_is_reported_not_failed():
    xml = _cx_chart([_cx_data(0, categories=["a"], values=[1])], [_cx_series("futureLayout")])
    (chart,) = _charts(_workbook_with_chartex(xml))

    assert "Part of this chart uses a chart type that is not extracted." in chart.text


def test_office_2016_chart_follows_the_chart_modes():
    xml = _waterfall().replace("Data!$B$2:$B$6", "Secret!$B$2:$B$6").replace("Data!$A$2:$A$6", "Secret!$A$2:$A$6")
    raw = _workbook_with_chartex(xml, hidden_sheet=True)

    (full,) = _charts(raw)
    assert "Source data: hidden worksheet 'Secret'." in full.text
    assert "| Start | 100 |" in full.text

    (withheld,) = _charts(raw, mode="without_hidden_data")
    assert "Source data: hidden worksheet 'Secret'; its values are not indexed." in withheld.text
    assert "Start" not in withheld.text
    assert "| 100" not in withheld.text
    # The totals note names positions once the labels are withheld.
    assert "Totals (bars showing the running total): point 1, point 5." in withheld.text

    assert _charts(raw, mode="off") == []


def test_invalid_office_2016_chart_is_skipped():
    blocks = _blocks(_workbook_with_chartex("<cx:chartSpace><broken>"))

    assert [block.metadata["xlsx_content_kind"] for block in blocks] == ["table"]


@pytest.mark.parametrize("kind", ["classic_uncached", "classic_cached", "office_2016"])
def test_without_hidden_data_mode_leaks_no_hidden_value_into_text_or_metadata(kind):
    """Every value of the hidden sheet stays out of the chunk, metadata included."""
    import json

    if kind == "office_2016":
        xml = _waterfall().replace("Data!", "Secret!")
        raw = _workbook_with_chartex(xml, hidden_sheet=True)
        hidden_values = ["Start", "Sales", "Costs", "Tax", "End", "Amount", "100", "115", "-30"]
    else:
        raw = _chart_over_hidden_sheet(cached=kind == "classic_cached")
        # The chart's own title and axis titles ("Monthly revenue", "Month") are chart text, kept.
        hidden_values = ["Jan", "Feb", "Mar", "2025", "2026", "100", "101.5", "202"]
    (chart,) = _charts(raw, mode="without_hidden_data")
    rendered = chart.text + json.dumps(chart.metadata)

    for value in hidden_values:
        assert value not in rendered, value
    assert chart.metadata["chart_hidden_data_withheld"] is True


# --- Office 2016 charts as Excel really writes them --------------------------
#
# Checked against Excel-made workbooks from LibreOffice's test suite
# (chart2/qa/extras/data/xlsx): Excel stores no cached values in cx: charts,
# references every range through a hidden ``_xlchart.vN.M`` defined name, names
# a dimension's header cell with ``cx:nf``, and gives map charts ``colorVal``.


def _excel_style_chartex(layout="waterfall", value_type="val", sheet="Data") -> tuple[str, str]:
    """An uncached cx: chart over A2:B4 and the defined names it relies on."""
    xml = _cx_chart(
        [
            '<cx:data id="0"><cx:strDim type="cat"><cx:f>_xlchart.v1.0</cx:f><cx:nf>_xlchart.v1.2</cx:nf></cx:strDim>'
            f'<cx:numDim type="{value_type}"><cx:f>_xlchart.v1.1</cx:f><cx:nf>_xlchart.v1.3</cx:nf></cx:numDim></cx:data>'
        ],
        [f'<cx:series layoutId="{layout}"><cx:dataId val="0"/></cx:series>'],
        title="Revenue",
    )
    names = (
        f'<definedName name="_xlchart.v1.0" hidden="1">{sheet}!$A$2:$A$4</definedName>'
        f'<definedName name="_xlchart.v1.1" hidden="1">{sheet}!$B$2:$B$4</definedName>'
        f'<definedName name="_xlchart.v1.2" hidden="1">{sheet}!$A$1</definedName>'
        f'<definedName name="_xlchart.v1.3" hidden="1">{sheet}!$B$1</definedName>'
    )
    return xml, names


def test_excel_style_office_2016_chart_reads_its_cells_through_internal_names():
    xml, names = _excel_style_chartex()
    (chart,) = _charts(_with_defined_names(_workbook_with_chartex(xml), names))

    # Header cells name the category column and the series (cx:nf).
    assert "| Month | 2025 |\n| --- | --- |\n| Jan | 100 |\n| Feb | 101.5 |\n| Mar | 3 |" in chart.text
    # Internal names are shown as the ranges they stand for, and link the table.
    assert "Data: categories Data!$A$2:$A$4; 2025 = Data!$B$2:$B$4" in chart.text
    assert "_xlchart" not in chart.text
    assert chart.metadata["chart_source_table_ids"] == ["sheet:0:table:0"]


def test_map_chart_values_are_its_colour_values():
    xml, names = _excel_style_chartex(layout="regionMap", value_type="colorVal")
    (chart,) = _charts(_with_defined_names(_workbook_with_chartex(xml), names))

    assert "Type: map chart" in chart.text
    assert "| Jan | 100 |" in chart.text


def test_excel_style_office_2016_chart_over_a_hidden_sheet_is_withheld_and_never_read(monkeypatch):
    import json

    from core.indexing.parsers.tabular.xlsx_drawings import extract

    sheets_read = []
    original = extract.resolve_ranges

    def recording(ranges, read_rows, *args, **kwargs):
        sheets_read.extend(cell_range.sheet for cell_range in ranges)
        return original(ranges, read_rows, *args, **kwargs)

    monkeypatch.setattr(extract, "resolve_ranges", recording)
    xml, names = _excel_style_chartex(sheet="Secret")
    raw = _with_defined_names(_workbook_with_chartex(xml, hidden_sheet=True), names)
    (chart,) = _charts(raw, mode="without_hidden_data")

    assert "Source data: hidden worksheet 'Secret'; its values are not indexed." in chart.text
    assert "Secret!$B$2:$B$4" in chart.text
    rendered = chart.text + json.dumps(chart.metadata)
    # Neither the values, the categories nor the header cells (cx:nf) leak.
    for hidden in ("Jan", "Feb", "Mar", "Month", "2025", "101.5", "| 100"):
        assert hidden not in rendered, hidden
    assert chart.metadata["chart_series"][0]["name"] == "Series 1"
    assert chart.metadata["chart_series"][0]["values_withheld"] is True
    assert "Secret" not in sheets_read


_LIBREOFFICE_SAMPLES = {
    # file: lines its chart text must contain (Excel-made unless noted)
    "waterfall2.xlsx": ["Type: waterfall chart", "Axes: Entry (categories); Cumulative change (values)", "| 3 | -6 |"],
    "treemap.xlsx": ["Type: treemap chart", "| Best / First / B | 4 |", "| Worst / Fourth | 3 |"],
    "sunburst.xlsx": ["Type: sunburst chart", "| Best / Second / D | 6 |"],
    "regionMap.xlsx": ["Type: map chart", "| Mali | 4 |"],
    "SimpleHistogram.xlsx": ["Type: histogram", "Excel groups these values into bins"],
    "paretoLine.xlsx": ["Type: Pareto chart"],
    "boxWhisker.xlsx": ["Type: box and whisker chart", "| Category | Series 1 | Series 2 | Series 3 |"],
    "funnel1.xlsx": ["Type: funnel chart", "| 4 | 12 |"],
    "color_funnel.xlsx": ["Chart: Colored funnel", "Type: funnel chart"],  # saved by Collabora Office
}


@pytest.mark.parametrize("name", sorted(_LIBREOFFICE_SAMPLES))
def test_real_office_2016_chart_samples(name):
    """Opt-in: set OPENRAG_CHARTEX_SAMPLES to a folder holding these files from
    LibreOffice core, chart2/qa/extras/data/xlsx (MPL-2.0, not vendored here)."""
    import os
    from pathlib import Path

    folder = os.environ.get("OPENRAG_CHARTEX_SAMPLES")
    if not folder or not (Path(folder) / name).is_file():
        pytest.skip("OPENRAG_CHARTEX_SAMPLES does not hold this sample")
    raw = (Path(folder) / name).read_bytes()
    blocks = _blocks(raw)
    charts = [block for block in blocks if block.metadata["xlsx_content_kind"] == "chart"]

    assert len(charts) == 1
    assert charts[0].metadata["chart_format"] == "chartex"
    assert "_xlchart" not in charts[0].text
    for expected in _LIBREOFFICE_SAMPLES[name]:
        assert expected in charts[0].text, expected
    assert any(block.metadata["xlsx_content_kind"] == "table" for block in blocks)


# --- pivot charts ------------------------------------------------------------


def _pivot_chart_workbook(pivot_name="[Sales.xlsx]Pivot!PivotTable1", sheet="Pivot") -> bytes:
    """A pivot table's rendered cells and a chart bound to it (c:pivotSource)."""
    from openpyxl.chart.pivot import PivotSource

    workbook = Workbook()
    pivot = workbook.active
    pivot.title = sheet
    for row in (["Region", "Sum of Sales"], ["North", 10], ["South", 20], ["Grand Total", 30]):
        pivot.append(row)
    chart = BarChart()
    chart.title = "Sales by region"
    chart.add_data(Reference(pivot, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(pivot, min_col=1, min_row=2, max_row=3))
    chart.pivotSource = PivotSource(name=pivot_name, fmtId=0)
    pivot.add_chart(chart, "D2")
    return _save(workbook)


def test_pivot_chart_names_its_pivot_table():
    (chart,) = _charts(_pivot_chart_workbook())

    assert "Pivot chart of pivot table PivotTable1 (worksheet Pivot)" in chart.text
    # The pivot table's rendered cells are what the chart plots; the grand
    # total row is not part of the chart.
    assert "| North | 10 |\n| South | 20 |" in chart.text
    assert "Grand Total" not in chart.text
    assert chart.metadata["chart_pivot_table"] == "PivotTable1"
    assert chart.metadata["chart_pivot_sheet"] == "Pivot"
    assert chart.metadata["chart_source_table_ids"] == ["sheet:0:table:0"]


def test_pivot_source_with_a_quoted_sheet_name():
    (chart,) = _charts(_pivot_chart_workbook("[Book1.xlsx]'Sales O''Brien'!PT 2", sheet="Sales O'Brien"))

    assert chart.metadata["chart_pivot_table"] == "PT 2"
    assert chart.metadata["chart_pivot_sheet"] == "Sales O'Brien"


def test_ordinary_chart_has_no_pivot_table():
    (chart,) = _charts(_workbook_with_chart(_monthly_revenue()))

    assert "Pivot chart" not in chart.text
    assert chart.metadata["chart_pivot_table"] is None


# --- charts inside grouped shapes --------------------------------------------


def _grouped(drawing: str, *, nested: bool = False) -> str:
    """Move every chart frame of the drawing into one group shape (grpSp) in the
    first anchor, next to a picture and a plain shape, as Excel writes a
    selection grouped with Ctrl+G; ``nested`` puts that group inside another."""
    import re

    frames = re.findall(r"<graphicFrame\b.*?</graphicFrame>", drawing, re.S)
    assert len(frames) == 2, drawing
    anchors = re.findall(r"<oneCellAnchor>.*?</oneCellAnchor>", drawing, re.S)
    first = anchors[0]
    group = (
        '<grpSp><nvGrpSpPr><cNvPr id="10" name="Group 1"/><cNvGrpSpPr/></nvGrpSpPr><grpSpPr/>'
        + "".join(frames)
        + '<sp><nvSpPr><cNvPr id="11" name="Arrow"/><cNvSpPr/></nvSpPr><spPr/></sp>'
        + '<pic><nvPicPr><cNvPr id="12" name="Logo"/><cNvPicPr/></nvPicPr><blipFill/><spPr/></pic>'
        + "</grpSp>"
    )
    if nested:
        group = f'<grpSp><nvGrpSpPr><cNvPr id="13" name="Outer"/><cNvGrpSpPr/></nvGrpSpPr><grpSpPr/>{group}</grpSp>'
    regrouped = first.replace(frames[0], group)
    drawing = drawing.replace(first, regrouped)
    return drawing.replace(anchors[1], "")


@pytest.mark.parametrize("nested", [False, True])
def test_charts_inside_a_group_are_each_indexed_at_the_group_position(nested):
    raw = _workbook_with_chart(charts=2)
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        drawing = archive.read("xl/drawings/drawing1.xml").decode()
    charts = _charts(_replace_parts(raw, {"xl/drawings/drawing1.xml": _grouped(drawing, nested=nested)}))

    assert [chart.metadata["chart_id"] for chart in charts] == ["sheet:0:chart:1", "sheet:0:chart:2"]
    assert all(chart.metadata["chart_anchor_range"] == "'Data'!E2" for chart in charts)
    assert all("| Jan | 100 | 200 |" in chart.text for chart in charts)


# --- other real-world details ------------------------------------------------


def test_series_filtered_out_of_the_chart_in_excel_are_not_indexed():
    """Unticking a series in Excel's chart filter moves it to an extension list
    (c15:filteredLineSeries); the chart no longer draws it."""
    filtered = (
        '<c:extLst><c:ext uri="{02D57815-91ED-43cb-92C2-25804820EDAC}" '
        'xmlns:c15="http://schemas.microsoft.com/office/drawing/2012/chart"><c15:filteredLineSeries>'
        + _series("Removed", ("Data!$A$2:$A$3", ["a", "b"]), ("Data!$C$2:$C$3", [98765, 43210]), index=1)
        + "</c15:filteredLineSeries></c:ext></c:extLst>"
    )
    xml = _chart_xml(
        [_plot("lineChart", [_series("Kept", ("Data!$A$2:$A$3", ["a", "b"]), ("Data!$B$2:$B$3", [1, 2]))], extra="")]
    ).replace("</c:lineChart>", filtered + "</c:lineChart>")
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| a | 1 |" in chart.text
    assert "Removed" not in chart.text
    assert "98765" not in chart.text


def test_charts_in_a_macro_enabled_workbook_are_indexed():
    raw = _workbook_with_chart(_monthly_revenue())
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        types = archive.read("[Content_Types].xml").decode()
    types = types.replace(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    )
    document = Document(
        filename="charts.xlsm",
        content_type=DocumentType.XLSX,
        raw_bytes=_replace_parts(raw, {"[Content_Types].xml": types}),
    )
    blocks = list(XlsxParser().iter_batches(document))

    assert [block.metadata["xlsx_content_kind"] for block in blocks] == ["table", "chart"]


def test_cached_charts_are_rendered_when_parsed_not_held_until_emitted(monkeypatch):
    """Memory stays bounded by rendered text, not by every chart's values."""
    from core.indexing.parsers.tabular.xlsx_drawings import extract

    rendered_before_emission = []
    original = extract.WorkbookCharts._render_entry

    def recording(self, entry, **kwargs):
        rendered_before_emission.append(self._emitted)
        return original(self, entry, **kwargs)

    monkeypatch.setattr(extract.WorkbookCharts, "_render_entry", recording)
    raw = _workbook_with_chart(charts=3)
    for index in range(1, 4):
        raw = _replace_parts(raw, {f"xl/charts/chart{index}.xml": _monthly_revenue()})
    charts = _charts(raw)

    assert [chart.metadata["chart_index"] for chart in charts] == [1, 2, 3]
    # All three were rendered while the workbook's charts were read, before any was emitted.
    assert rendered_before_emission == [0, 0, 0]


def test_workbook_chart_budget_skips_later_charts():
    raw = _workbook_with_chart(charts=3)
    for index in range(1, 4):
        raw = _replace_parts(raw, {f"xl/charts/chart{index}.xml": _monthly_revenue()})
    size = len(_monthly_revenue().encode())
    charts = _charts(raw, max_chart_bytes=2 * size + 10)

    assert [chart.metadata["chart_id"] for chart in charts] == ["sheet:0:chart:1", "sheet:0:chart:2"]
    assert all(chart.metadata["workbook_chart_issue_count"] == 1 for chart in charts)


def test_default_part_limit_accepts_a_large_cached_chart():
    """20 series x 5,000 cached points (about 8 MB) is indexed with the defaults."""
    points = 5000
    categories = (f"Data!$A$2:$A${points + 1}", [f"d{index}" for index in range(points)])
    series = [
        _series(f"S{number}", categories, (f"Data!$B$2:$B${points + 1}", list(range(points))), index=number)
        for number in range(20)
    ]
    xml = _chart_xml([_plot("lineChart", series)])
    assert len(xml.encode()) > 4 * 1024 * 1024
    (chart,) = _charts(_workbook_with_chart(xml))

    assert chart.metadata["chart_series_count"] == 20
    assert chart.metadata["chart_point_count"] == 20 * points


# --- dates written as General (XlsxWriter, pandas) ---------------------------


def test_general_cached_dates_on_a_date_axis_are_dates():
    xml = _dated_chart(format_code="General")
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| 2025-01-01 | 10 |" in chart.text


def test_general_cached_dates_on_a_category_axis_follow_their_cells_format():
    """XlsxWriter caches dates as General on an ordinary category axis; Excel
    then labels them with the cells' date format, read from the workbook."""
    from datetime import date

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["Day", "Visits"])
    for day, visits in ((date(2025, 1, 1), 5), (date(2025, 1, 2), 7)):
        sheet.append([day, visits])
    for row in (2, 3):
        sheet.cell(row=row, column=1).number_format = "yyyy-mm-dd"
    _add_chart(sheet, sheet)
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f"<c:cat>{_num_cache_ref('Data!$A$2:$A$3', [45658, 45659], 'General')}</c:cat>"
        f"<c:val>{_num_cache_ref('Data!$B$2:$B$3', [5, 7], 'General')}</c:val></c:ser>"
    )
    xml = _chart_xml([_plot("lineChart", [series])], axes=_axis("catAx", 1) + _axis("valAx", 2))
    (chart,) = _charts(_replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml}))

    assert "| 2025-01-01 | 5 |" in chart.text
    assert "| 2025-01-02 | 7 |" in chart.text


def test_general_cached_numbers_over_number_cells_stay_numbers():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["Year", "Sales"])
    for year, sales in ((2024, 5), (2025, 7)):
        sheet.append([year, sales])
    _add_chart(sheet, sheet)
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f"<c:cat>{_num_cache_ref('Data!$A$2:$A$3', [2024, 2025], 'General')}</c:cat>"
        f"<c:val>{_num_cache_ref('Data!$B$2:$B$3', [5, 7], 'General')}</c:val></c:ser>"
    )
    xml = _chart_xml([_plot("lineChart", [series])], axes=_axis("catAx", 1) + _axis("valAx", 2))
    (chart,) = _charts(_replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml}))

    assert "| 2024 | 5 |" in chart.text


# --- combination charts, category headers, value formats from cells ---------


def _combo(target_categories=("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"])) -> str:
    months = ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"])
    return _chart_xml(
        [
            _plot("barChart", [_series("Actual", months, ("Data!$B$2:$B$4", [100, 101.5, 3]))]),
            _plot(
                "lineChart",
                [
                    _series(
                        "Target", target_categories, ("Data!$C$2:$C$4", [200, 201, 202]), index=1, name_ref="Data!$C$1"
                    )
                ],
            ),
        ],
        title="Actual vs target",
    )


def _separators(text: str) -> int:
    return sum(line.startswith("| ---") for line in text.splitlines())


def test_combination_chart_over_the_same_categories_is_one_table():
    """Columns for actuals and a line for targets: one row per month, both values."""
    (chart,) = _charts(_workbook_with_chart(_combo()))

    assert "| Month | Actual | Target |" in chart.text
    assert "| Feb | 101.5 | 201 |" in chart.text
    assert _separators(chart.text) == 1


def test_combination_chart_over_different_categories_keeps_two_tables():
    (chart,) = _charts(_workbook_with_chart(_combo(("Data!$A$2:$A$4", ["Q1", "Q2", "Q3"]))))

    assert _separators(chart.text) == 2
    assert "| Q2 | 201 |" in chart.text


def test_category_and_xy_plots_never_share_a_table():
    """Same labels, different meanings: categories are not x values."""
    xml = _chart_xml(
        [
            _plot("barChart", [_series("Bars", ("Data!$A$2:$A$4", [1, 2, 3]), ("Data!$B$2:$B$4", [4, 5, 6]))]),
            _plot(
                "scatterChart",
                [_series("Points", ("Data!$A$2:$A$4", [1, 2, 3]), ("Data!$C$2:$C$4", [7, 8, 9]), index=1, xy=True)],
            ),
        ]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert _separators(chart.text) == 2
    assert "| X | Points |" in chart.text


def test_unnamed_series_are_numbered_across_plots():
    months = ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"])
    xml = _chart_xml(
        [
            _plot("barChart", [_series(None, months, ("Data!$B$2:$B$4", [1, 2, 3]))]),
            _plot("lineChart", [_series(None, months, ("Data!$C$2:$C$4", [4, 5, 6]), index=1)]),
        ]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| Category | Series 1 | Series 2 |" in chart.text
    assert [entry["name"] for entry in chart.metadata["chart_series"]] == ["Series 1", "Series 2"]


def test_category_header_comes_from_the_header_row_of_a_cached_chart():
    xml = _chart_xml(
        [_plot("barChart", [_series("2025", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), ("Data!$B$2:$B$4", [1, 2, 3]))])]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| Month | 2025 |" in chart.text


def test_category_header_is_not_guessed_when_names_are_not_above_their_values():
    xml = _chart_xml(
        [
            _plot(
                "barChart",
                [
                    _series(
                        "2025",
                        ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]),
                        ("Data!$B$2:$B$4", [1, 2, 3]),
                        name_ref="Data!$B$10",
                    )
                ],
            )
        ]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| Category | 2025 |" in chart.text


def test_axis_title_names_the_category_column_before_the_header_cell():
    xml = _chart_xml(
        [
            _plot(
                "barChart", [_series("2025", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), ("Data!$B$2:$B$4", [1, 2, 3]))]
            )
        ],
        axes=_axis("catAx", 1, "Period") + _axis("valAx", 2),
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| Period | 2025 |" in chart.text


def test_header_on_a_hidden_row_is_left_out_without_hidden_data():
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    sheet.row_dimensions[1].hidden = True
    _add_chart(sheet, sheet)
    xml = _chart_xml(
        [_plot("barChart", [_series("2025", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), ("Data!$B$2:$B$4", [1, 2, 3]))])]
    )
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml})

    (withheld,) = _charts(raw, mode="without_hidden_data")
    (full,) = _charts(raw)

    # The header and the series name both sit on the hidden row.
    assert "| Category | Series 1 |" in withheld.text
    assert "2025" not in withheld.text
    assert "| Month | 2025 |" in full.text


def _margin_workbook(cache_format="General") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    for row in (["Month", "Margin"], ["Jan", 0.312], ["Feb", 0.287]):
        sheet.append(row)
    for row in (2, 3):
        sheet.cell(row=row, column=2).number_format = "0.0%"
    _add_chart(sheet, sheet)
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        '<c:tx><c:strRef><c:f>Data!$B$1</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>Margin</c:v></c:pt>'
        "</c:strCache></c:strRef></c:tx>"
        f"<c:cat>{_ref('Data!$A$2:$A$3', ['Jan', 'Feb'], numeric=False)}</c:cat>"
        f"<c:val>{_num_cache_ref('Data!$B$2:$B$3', [0.312, 0.287], cache_format)}</c:val></c:ser>"
    )
    xml = _chart_xml([_plot("lineChart", [series])])
    return _replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml})


def test_values_cached_as_general_take_their_cells_display_format():
    """XlsxWriter caches percentages as General; Excel shows them as the cells do."""
    (chart,) = _charts(_margin_workbook())

    assert "Margin shown as percentage (0.312 -> 31.2%)" in chart.text
    assert "| Month | Margin |" in chart.text
    assert "| Feb | 0.287 |" in chart.text


def test_an_explicit_cached_value_format_wins_over_the_cells():
    (chart,) = _charts(_margin_workbook(cache_format="0.000"))

    assert "Display:" not in chart.text


def test_uncached_values_take_their_cells_display_format():
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    for row in (2, 3, 4):
        sheet.cell(row=row, column=2).number_format = "0.0%"
    _add_chart(sheet, sheet)
    (chart,) = _charts(_save(workbook))

    assert "2025 shown as percentage" in chart.text


def _counting_parses(monkeypatch) -> list[int]:
    from core.indexing.parsers.tabular.xlsx_drawings import extract

    calls: list[int] = []
    original = extract.parse_chart

    def counting(root):
        calls.append(1)
        return original(root)

    monkeypatch.setattr(extract, "parse_chart", counting)
    return calls


def test_header_and_value_format_lookups_never_parse_a_chart_twice(monkeypatch):
    """They change only the heading: the chart is rendered as parsed, then completed."""
    from core.indexing.parsers.tabular.xlsx_drawings import extract

    calls = _counting_parses(monkeypatch)
    monkeypatch.setattr(extract, "_MAX_HELD_POINTS", 0)
    (chart,) = _charts(_margin_workbook())

    assert len(calls) == 1
    assert "\x00" not in chart.text
    assert "| Month | Margin |" in chart.text
    assert chart.metadata["chart_series"][0]["display_format"]["kind"] == "percentage"


def _general_dates_workbook() -> bytes:
    from datetime import date

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["Day", "Visits"])
    for day, visits in ((date(2025, 1, 1), 5), (date(2025, 1, 2), 7)):
        sheet.append([day, visits])
    for row in (2, 3):
        sheet.cell(row=row, column=1).number_format = "yyyy-mm-dd"
    _add_chart(sheet, sheet)
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f"<c:cat>{_num_cache_ref('Data!$A$2:$A$3', [45658, 45659], 'General')}</c:cat>"
        f"<c:val>{_num_cache_ref('Data!$B$2:$B$3', [5, 7], 'General')}</c:val></c:ser>"
    )
    xml = _chart_xml([_plot("lineChart", [series])], axes=_axis("catAx", 1) + _axis("valAx", 2))
    return _replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml})


def test_charts_past_the_held_point_budget_are_parsed_again_with_the_same_text(monkeypatch):
    """Date categories rewrite every row, so the chart waits for its cells' format:
    held in memory within the budget, parsed again past it."""
    from core.indexing.parsers.tabular.xlsx_drawings import extract

    calls = _counting_parses(monkeypatch)
    (held,) = _charts(_general_dates_workbook())
    held_calls = len(calls)
    monkeypatch.setattr(extract, "_MAX_HELD_POINTS", 0)
    (reparsed,) = _charts(_general_dates_workbook())

    assert held_calls == 1
    assert len(calls) - held_calls == 2
    assert reparsed.text == held.text
    assert "| 2025-01-01 | 5 |" in held.text


def test_a_value_series_formatted_as_dates_keeps_its_numbers():
    """Rows are rendered before value formats are read; only display formats apply."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    for row in (["Task", "Start"], ["A", 45658], ["B", 45659]):
        sheet.append(row)
    for row in (2, 3):
        sheet.cell(row=row, column=2).number_format = "yyyy-mm-dd"
    _add_chart(sheet, sheet)
    series = (
        '<c:ser><c:idx val="0"/><c:order val="0"/>'
        f"<c:cat>{_ref('Data!$A$2:$A$3', ['A', 'B'], numeric=False)}</c:cat>"
        f"<c:val>{_num_cache_ref('Data!$B$2:$B$3', [45658, 45659], 'General')}</c:val></c:ser>"
    )
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": _chart_xml([_plot("barChart", [series])])})
    (chart,) = _charts(raw)

    assert "| A | 45658 |" in chart.text
    assert "Display:" not in chart.text


# --- hidden rows and columns of visible sheets; unreadable chart parts -------


def _data_with_hidden_row(row: int) -> Workbook:
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    sheet.row_dimensions[row].hidden = True
    _add_chart(sheet, sheet)
    return workbook


def test_series_name_and_title_on_hidden_cells_are_withheld():
    workbook = _data_with_hidden_row(3)
    title = (
        '<c:title><c:tx><c:strRef><c:f>Data!$A$3</c:f><c:strCache><c:ptCount val="1"/>'
        '<c:pt idx="0"><c:v>Secret title</c:v></c:pt></c:strCache></c:strRef></c:tx></c:title>'
    )
    series = _series("Secret name", ("Data!$A$2:$A$2", ["Jan"]), ("Data!$B$2:$B$2", [1]), name_ref="Data!$C$3")
    xml = _chart_xml([_plot("barChart", [series])]).replace("<c:chart>", "<c:chart>" + title, 1)
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml})

    (withheld,) = _charts(raw, mode="without_hidden_data")
    (full,) = _charts(raw)

    assert "Secret" not in withheld.text
    assert "Secret title" in full.text and "Secret name" in full.text


def test_a_hidden_row_leaves_a_two_level_category_cache_with_its_point():
    """One cached label per row: the hidden row's point leaves from the cache."""
    workbook = _data_with_hidden_row(3)
    series = _series("S", ("Data!$A$2:$B$4", ["Jan", "SECRET", "Mar"]), ("Data!$C$2:$C$4", [200, 201, 202]))
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": _chart_xml([_plot("barChart", [series])])})

    (chart,) = _charts(raw, mode="without_hidden_data")

    assert "SECRET" not in chart.text and "201" not in chart.text
    assert "| Jan | 200 |" in chart.text and "| Mar | 202 |" in chart.text


def test_a_hidden_label_level_is_read_again_from_the_visible_cells():
    """A combined cached label cannot lose one level: the visible cells are read."""
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    sheet.column_dimensions["B"].hidden = True
    _add_chart(sheet, sheet)
    series = _series(
        "S", ("Data!$A$2:$B$4", ["Jan / SECRET", "Feb / SECRET", "Mar / SECRET"]), ("Data!$C$2:$C$4", [200, 201, 202])
    )
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": _chart_xml([_plot("barChart", [series])])})

    (chart,) = _charts(raw, mode="without_hidden_data")

    assert "SECRET" not in chart.text
    assert "| Feb | 201 |" in chart.text


def _corrupt_member(raw: bytes, name: str) -> bytes:
    """Damage a member's compressed bytes, leaving the package listing intact."""
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        info = archive.getinfo(name)
    data = bytearray(raw)
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    for offset in range(start + 2, start + min(info.compress_size, 40)):
        data[offset] ^= 0xFF
    return bytes(data)


def test_an_unreadable_chart_part_skips_that_chart_only():
    raw = _workbook_with_chart(charts=2)
    raw = _replace_parts(raw, {"xl/charts/chart2.xml": _monthly_revenue()})
    raw = _corrupt_member(raw, "xl/charts/chart1.xml")
    blocks = _blocks(raw)
    charts = [block for block in blocks if block.metadata["xlsx_content_kind"] == "chart"]

    assert any(block.metadata["xlsx_content_kind"] == "table" for block in blocks)
    assert [chart.metadata["chart_title"] for chart in charts] == ["Monthly revenue"]
    assert charts[0].metadata["workbook_chart_issue_count"] == 1
    assert charts[0].metadata["workbook_chart_extraction"] == "partial"


def test_a_title_or_name_read_from_a_hidden_sheet_is_reported_in_full_mode():
    """Full mode keeps the text, and still names the hidden sheet it comes from."""
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    labels = workbook.create_sheet("Labels")
    labels.append(["Board title", "Board series"])
    labels.sheet_state = "hidden"
    _add_chart(sheet, sheet)
    title = (
        '<c:title><c:tx><c:strRef><c:f>Labels!$A$1</c:f><c:strCache><c:ptCount val="1"/>'
        '<c:pt idx="0"><c:v>Board title</c:v></c:pt></c:strCache></c:strRef></c:tx></c:title>'
    )
    series = _series(
        "Board series", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), ("Data!$B$2:$B$4", [1, 2, 3]), name_ref="Labels!$B$1"
    )
    xml = _chart_xml([_plot("barChart", [series])]).replace("<c:chart>", "<c:chart>" + title, 1)
    (chart,) = _charts(_replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml}), max_chart_points_from_tables=1)

    assert "Board title" in chart.text and "Board series" in chart.text
    assert "Source data: hidden worksheet 'Labels'." in chart.text
    assert chart.metadata["chart_hidden_source_sheets"] == ["Labels"]
    # A chart reading a hidden sheet is never treated as already indexed.
    assert chart.metadata["chart_data_in_tables"] is False


# --- text boxes, text drawn on charts, data labels, trendlines ---------------

_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def _text_box(text, *, cells=((5, 1), (7, 3)), hidden=False, textlink="", grouped=False) -> str:
    (from_col, from_row), (to_col, to_row) = cells
    shape = (
        f'<sp macro="" textlink="{textlink}"><nvSpPr><cNvPr id="9" name="TextBox 1"'
        f'{' hidden="1"' if hidden else ""}/><cNvSpPr txBox="1"/></nvSpPr><spPr/>'
        f"<txBody><a:bodyPr/>{''.join(f'<a:p><a:r><a:t>{line}</a:t></a:r></a:p>' for line in text.split(chr(10)))}"
        "</txBody></sp>"
    )
    if grouped:
        shape = f"<grpSp><nvGrpSpPr><cNvPr id='8' name='Group'/><cNvGrpSpPr/></nvGrpSpPr><grpSpPr/>{shape}</grpSp>"
    return (
        f"<twoCellAnchor {_A}><from><col>{from_col}</col><colOff>0</colOff><row>{from_row}</row><rowOff>0</rowOff>"
        f"</from><to><col>{to_col}</col><colOff>0</colOff><row>{to_row}</row><rowOff>0</rowOff></to>"
        f"{shape}<clientData/></twoCellAnchor>"
    )


def _with_text_boxes(raw: bytes, *boxes: str) -> bytes:
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        drawing = archive.read("xl/drawings/drawing1.xml").decode()
    return _replace_parts(raw, {"xl/drawings/drawing1.xml": drawing.replace("</wsDr>", "".join(boxes) + "</wsDr>")})


def _with_chart_text(raw: bytes, text: str, *, hidden=False) -> bytes:
    shapes = (
        '<c:userShapes xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
        f'xmlns:cdr="http://schemas.openxmlformats.org/drawingml/2006/chartDrawing" {_A}>'
        "<cdr:relSizeAnchor><cdr:from><cdr:x>0.1</cdr:x><cdr:y>0.1</cdr:y></cdr:from>"
        "<cdr:to><cdr:x>0.4</cdr:x><cdr:y>0.2</cdr:y></cdr:to>"
        f'<cdr:sp macro="" textlink=""><cdr:nvSpPr><cdr:cNvPr id="2" name="TextBox 1"{' hidden="1"' if hidden else ""}/>'
        '<cdr:cNvSpPr txBox="1"/></cdr:nvSpPr><cdr:spPr/>'
        f"<cdr:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></cdr:txBody></cdr:sp>"
        "</cdr:relSizeAnchor></c:userShapes>"
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="../drawings/drawing2.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chartUserShapes"/></Relationships>'
    )
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        types = archive.read("[Content_Types].xml").decode()
        chart = archive.read("xl/charts/chart1.xml").decode()
    types = types.replace(
        "</Types>",
        '<Override PartName="/xl/drawings/drawing2.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.drawingml.chartshapes+xml"/></Types>',
    )
    return _replace_parts(
        raw,
        {
            "xl/drawings/drawing2.xml": shapes,
            "xl/charts/chart1.xml": chart.replace(
                "</c:chartSpace>",
                '<c:userShapes xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                'r:id="rId1"/></c:chartSpace>',
            ),
            "xl/charts/_rels/chart1.xml.rels": rels,
            "[Content_Types].xml": types,
        },
    )


def _kinds(blocks) -> list[str]:
    return [block.metadata["xlsx_content_kind"] for block in blocks]


def _text_boxes(blocks) -> list:
    return [block for block in blocks if block.metadata["xlsx_content_kind"] == "text_box"]


def test_worksheet_text_boxes_are_indexed_after_the_sheet_charts():
    raw = _with_text_boxes(
        _workbook_with_chart(_monthly_revenue()),
        _text_box("Q3 dip:\nsupplier strike"),
        _text_box("Grouped note", cells=((1, 20), (3, 22)), grouped=True),
    )
    blocks = _blocks(raw)
    (box,) = _text_boxes(blocks)

    assert _kinds(blocks) == ["table", "chart", "text_box"]
    assert box.text.startswith("Text boxes on worksheet Data:")
    assert "- Over F2:H4: Q3 dip:\n  supplier strike" in box.text
    assert "- Over B21:D23: Grouped note" in box.text
    assert box.metadata["text_box_count"] == 2
    assert box.metadata["text_box_anchors"] == ["F2:H4", "B21:D23"]


def test_text_boxes_are_indexed_when_charts_are_off():
    raw = _with_text_boxes(_workbook_with_chart(_monthly_revenue()), _text_box("Note on the data"))

    assert _kinds(_blocks(raw, mode="off")) == ["table", "text_box"]


def test_hidden_text_boxes_and_links_to_hidden_cells_are_withheld():
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    sheet.row_dimensions[3].hidden = True
    _add_chart(sheet, sheet)
    raw = _with_text_boxes(
        _save(workbook),
        _text_box("Visible note"),
        _text_box("Hidden note", hidden=True),
        _text_box("101.5", textlink="$B$3"),
    )

    (withheld,) = _text_boxes(_blocks(raw, mode="without_hidden_data"))
    (full,) = _text_boxes(_blocks(raw))
    (off,) = _text_boxes(_blocks(raw, mode="off"))

    assert "Visible note" in withheld.text
    assert "Hidden note" not in withheld.text and "101.5" not in withheld.text
    assert "(hidden in Excel) Hidden note" in full.text and "101.5" in full.text
    assert off.text == withheld.text


def test_text_drawn_on_a_chart_is_part_of_its_chunk():
    raw = _with_chart_text(_workbook_with_chart(_monthly_revenue()), "Target revised in June")
    blocks = _blocks(raw)
    (chart,) = [block for block in blocks if block.metadata["xlsx_content_kind"] == "chart"]

    assert 'Text on the chart: "Target revised in June"' in chart.text
    assert "text_box" not in _kinds(blocks)


def test_hidden_text_on_a_chart_is_withheld_without_hidden_data():
    raw = _with_chart_text(_workbook_with_chart(_monthly_revenue()), "Draft figure", hidden=True)

    (withheld,) = _charts(raw, mode="without_hidden_data")
    (full,) = _charts(raw)

    assert "Draft figure" not in withheld.text
    assert "Draft figure" in full.text


def _labelled_series(labels_xml: str, ext_xml: str = "") -> str:
    months = ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"])
    series = _series("2025", months, ("Data!$B$2:$B$4", [100, 101.5, 3]))
    return series.replace("</c:ser>", f"<c:dLbls>{labels_xml}</c:dLbls>{ext_xml}</c:ser>")


def _custom_label(index: int, text: str) -> str:
    return (
        f'<c:dLbl><c:idx val="{index}"/><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p>'
        '</c:rich></c:tx><c:showVal val="1"/></c:dLbl>'
    )


def test_typed_data_labels_get_a_column_next_to_their_values():
    xml = _chart_xml(
        [_plot("barChart", [_labelled_series(_custom_label(1, "Supplier strike") + '<c:showVal val="1"/>')])]
    )
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| Month | 2025 | 2025 (data label) |" in chart.text
    assert "| Feb | 101.5 | Supplier strike |" in chart.text
    assert "| Jan | 100 |  |" in chart.text


def test_automatic_value_labels_add_no_column():
    xml = _chart_xml([_plot("barChart", [_labelled_series('<c:showVal val="1"/>')])])
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "(data label)" not in chart.text


def test_data_labels_from_cells_are_read_from_their_cache():
    ext = (
        '<c:extLst><c:ext uri="{02D57815-91ED-43cb-92C2-25804820EDAC}">'
        '<c15:datalabelsRange xmlns:c15="http://schemas.microsoft.com/office/drawing/2012/chart">'
        "<c15:f>Data!$C$2:$C$4</c15:f><c15:dlblRangeCache>"
        f"{_points(['North', 'South', 'East'])}</c15:dlblRangeCache></c15:datalabelsRange></c:ext></c:extLst>"
    )
    shown = (
        '<c:extLst><c:ext uri="{CE6537A1-D6FC-4f65-9D91-7224C49458BB}">'
        '<c15:showDataLabelsRange xmlns:c15="http://schemas.microsoft.com/office/drawing/2012/chart" val="1"/>'
        "</c:ext></c:extLst>"
    )
    xml = _chart_xml([_plot("barChart", [_labelled_series(shown, ext)])])
    (chart,) = _charts(_workbook_with_chart(xml))

    assert "| Mar | 3 | East |" in chart.text


def test_a_data_label_leaves_with_its_hidden_point():
    workbook = _data_with_hidden_row(3)
    xml = _chart_xml(
        [_plot("barChart", [_labelled_series(_custom_label(1, "Feb note") + _custom_label(2, "Mar note"))])]
    )
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml})

    (chart,) = _charts(raw, mode="without_hidden_data")

    assert "Feb note" not in chart.text
    assert "| Mar | 3 | Mar note |" in chart.text


def test_linked_data_label_does_not_expose_a_hidden_cell():
    workbook = _data_with_hidden_row(3)
    linked = (
        '<c:dLbl><c:idx val="0"/><c:tx><c:strRef><c:f>Data!$C$3</c:f>'
        f"<c:strCache>{_points(['Confidential label'])}</c:strCache></c:strRef></c:tx></c:dLbl>"
    )
    xml = _chart_xml([_plot("barChart", [_labelled_series(linked + _custom_label(2, "Mar note"))])])
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml})

    (full,) = _charts(raw)
    (withheld,) = _charts(raw, mode="without_hidden_data")

    assert "| Jan | 100 | Confidential label |" in full.text
    assert "| Jan | 100 |  |" in withheld.text
    assert "Confidential label" not in withheld.text
    assert "| Mar | 3 | Mar note |" in withheld.text


def test_drawing_discovery_stops_reading_when_workbook_budget_is_exhausted():
    from core.indexing.parsers.tabular.xlsx_drawings.package import locate_charts

    raw = _with_text_boxes(_workbook_with_chart(_monthly_revenue()), _text_box("Long drawing note " * 500))
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        budget = archive.getinfo("xl/workbook.xml").file_size + archive.getinfo("xl/_rels/workbook.xml.rels").file_size
        inventory = locate_charts(archive, max_charts=10, max_part_bytes=64_000, max_total_bytes=budget)

    assert not inventory.placements and not inventory.text_boxes
    assert len([reason for reason in inventory.skipped if "discovery budget exhausted" in reason]) == 1


def test_trendlines_are_named():
    months = ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"])
    series = _series("2025", months, ("Data!$B$2:$B$4", [100, 101.5, 3])).replace(
        "</c:ser>",
        '<c:trendline><c:name>Trend</c:name><c:trendlineType val="movingAvg"/><c:period val="2"/></c:trendline>'
        '<c:trendline><c:trendlineType val="linear"/></c:trendline></c:ser>',
    )
    (chart,) = _charts(_workbook_with_chart(_chart_xml([_plot("lineChart", [series])])))

    assert "Trendlines: 2025: Trend (moving average (period 2) trendline), linear trendline." in chart.text


# --- one visibility decision per point; what counts as already indexed -------


def test_waterfall_totals_move_with_the_points_left_out():
    """Start (a total) sits on a hidden row: End stays named, Sales is not a total."""
    base = _save(_data_with_hidden_row(2))
    (chart,) = _charts(_workbook_with_chartex(_waterfall(), base=base))

    assert "Totals (bars showing the running total): End." in chart.text
    assert "| Start |" not in chart.text


def test_offset_category_and_value_ranges_stay_paired_around_a_hidden_row():
    workbook = _data_with_hidden_row(3)
    series = _series("S", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), ("Data!$C$5:$C$7", [10, 20, 30]))
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": _chart_xml([_plot("barChart", [series])])})

    (chart,) = _charts(raw)

    assert "| Jan | 10 |" in chart.text and "| Mar | 30 |" in chart.text
    assert "| 20 |" not in chart.text and "Feb" not in chart.text


def _literal_values_series() -> str:
    literal = '<c:numLit><c:ptCount val="3"/>' + "".join(
        f'<c:pt idx="{index}"><c:v>{value}</c:v></c:pt>' for index, value in enumerate((7, 8, 9))
    )
    series = _series("Typed", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), ("Data!$B$2:$B$4", [1, 2, 3]))
    return series.replace(
        '<c:val><c:numRef><c:f>Data!$B$2:$B$4</c:f><c:numCache><c:ptCount val="3"/>'
        '<c:pt idx="0"><c:v>1</c:v></c:pt><c:pt idx="1"><c:v>2</c:v></c:pt><c:pt idx="2"><c:v>3</c:v></c:pt>'
        "</c:numCache></c:numRef></c:val>",
        f"<c:val>{literal}</c:numLit></c:val>",
    )


def test_literal_values_keep_the_chart_data_over_an_indexed_table():
    """The categories are in a table, the typed values are not: the data stays."""
    xml = _chart_xml([_plot("barChart", [_literal_values_series()])])
    (chart,) = _charts(_workbook_with_chart(xml), max_chart_points_from_tables=1)

    assert chart.metadata["chart_data_in_tables"] is False
    assert "| Feb | 8 |" in chart.text


def test_text_data_labels_keep_the_chart_data_over_an_indexed_table():
    xml = _chart_xml([_plot("barChart", [_labelled_series(_custom_label(1, "Supplier strike"))])])
    (chart,) = _charts(_workbook_with_chart(xml), max_chart_points_from_tables=1)

    assert chart.metadata["chart_data_in_tables"] is False
    assert "Supplier strike" in chart.text


def test_a_chart_plotting_cells_its_table_left_out_keeps_its_data():
    """plotVisOnly off: the chart shows a row hidden by hand, which no table indexed."""
    workbook = _data_with_hidden_row(3)
    months = ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"])
    xml = _chart_xml([_plot("barChart", [_series("2025", months, ("Data!$B$2:$B$4", [100, 101.5, 3]))])]).replace(
        '<c:plotVisOnly val="1"/>', '<c:plotVisOnly val="0"/>'
    )
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": xml})
    (chart,) = _charts(raw, max_chart_points_from_tables=1)

    assert chart.metadata["chart_data_in_tables"] is False
    assert "| Feb | 101.5 |" in chart.text


def test_a_chart_section_longer_than_a_chunk_is_split_with_the_chart_context():
    """A notes paragraph (300 waterfall totals) or a heading over the budget no
    longer makes one oversized piece: each part fits and names its chart."""
    from core.chunking.markdown_utils import chunk_xlsx_chart

    def words(text: str) -> int:
        return len(text.split())

    totals = "Totals (bars showing the running total): " + ", ".join(f"step{index}" for index in range(300)) + "."
    content = "Chart: Cash flow\nType: waterfall chart\n\n| Step | Amount |\n| --- | --- |\n| A | 1 |\n\n" + totals
    context = "Chart: Cash flow (waterfall chart), worksheet Data"
    pieces = chunk_xlsx_chart(content, {"chart_context": context}, 60, words)

    assert all(words(piece) <= 60 for piece in pieces)
    assert all(piece.startswith(context) for piece in pieces[1:])
    assert "step0" in "".join(pieces) and "step299" in "".join(pieces)

    heading = "Chart: " + " ".join(f"title{index}" for index in range(100))
    pieces = chunk_xlsx_chart(
        heading + "\n\n| X | Y |\n| --- | --- |\n| a | 1 |", {"chart_context": context}, 40, words
    )
    assert all(words(piece) <= 40 for piece in pieces)
    assert "| a | 1 |" in pieces[-1]


@pytest.mark.parametrize("column", ["-2", "-1", "16384", "99999999999"])
def test_a_drawing_marker_outside_the_grid_names_no_cells(column):
    """A negative column made the column-name loop spin forever; now the anchor
    is simply unknown and the text box is still indexed."""
    import signal

    box = _text_box("Note").replace("<col>5</col>", f"<col>{column}</col>", 1)
    raw = _with_text_boxes(_workbook_with_chart(_monthly_revenue()), box)
    signal.signal(signal.SIGALRM, lambda *_: pytest.fail("drawing discovery did not terminate"))
    signal.alarm(5)
    try:
        (found,) = _text_boxes(_blocks(raw))
    finally:
        signal.alarm(0)

    assert "Note" in found.text
    assert found.metadata["text_box_anchors"] == []


def test_cached_values_on_a_hidden_sheet_follow_the_series_points_in_full_mode():
    """Visible categories over a hidden row, values cached on a hidden sheet:
    full mode keeps the hidden sheet's data, but drops the hidden point from both."""
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    sheet.row_dimensions[3].hidden = True
    secret = workbook.create_sheet("Secret")
    for value in ("Amount", 100, 101.5, 3):
        secret.append([None, value])
    secret.sheet_state = "hidden"
    _add_chart(sheet, sheet)
    series = _series("S", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), ("Secret!$B$2:$B$4", [100, 101.5, 3]))
    raw = _replace_parts(_save(workbook), {"xl/charts/chart1.xml": _chart_xml([_plot("barChart", [series])])})

    (chart,) = _charts(raw)

    assert "| Jan | 100 |" in chart.text and "| Mar | 3 |" in chart.text
    assert "101.5" not in chart.text and "Source data: hidden worksheet 'Secret'." in chart.text


def test_a_chart_context_longer_than_half_the_budget_is_shortened():
    from core.chunking.markdown_utils import chunk_xlsx_chart

    def words(text: str) -> int:
        return len(text.split())

    context = "Chart: " + " ".join(f"word{index}" for index in range(80)) + " (line chart), worksheet Data"
    rows = "\n".join(f"| r{index} | {index} |" for index in range(40))
    content = f"{context}\n\n| X | Y |\n| --- | --- |\n{rows}"
    pieces = chunk_xlsx_chart(content, {"chart_context": context}, 30, words)

    assert len(pieces) > 1
    assert all(words(piece) <= 30 for piece in pieces)
    assert all(piece.startswith("Chart: word0") for piece in pieces[1:])
    assert all("…" in piece.splitlines()[0] for piece in pieces[1:])
    assert "| r39 | 39 |" in pieces[-1]


# --- read budget, hidden chart objects, metadata caps, workbook status --------


def _two_distant_uncached_charts() -> bytes:
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    for offset, row in enumerate((["Apr", 7, 8], ["May", 9, 10], ["Jun", 11, 12])):
        for column, value in enumerate(row, 1):
            sheet.cell(row=2000 + offset, column=column, value=value)
    for anchor, (first, last) in (("E2", (2, 4)), ("E30", (2000, 2002))):
        chart = BarChart()
        chart.title = f"Rows {first}-{last}"
        chart.add_data(Reference(sheet, min_col=2, min_row=first, max_row=last), titles_from_data=False)
        chart.set_categories(Reference(sheet, min_col=1, min_row=first, max_row=last))
        sheet.add_chart(chart, anchor)
    return _save(workbook)


def test_chart_reads_stop_at_the_workbook_read_budget_and_say_so():
    """Each read parses its sheet from row 1: a window costs its last row. The
    far chart does not fit in the budget; it is reported, not counted as broken."""
    near, far = _charts(_two_distant_uncached_charts(), max_chart_scan_rows=10)

    assert "| Jan | 100 |" in near.text and near.metadata["chart_ranges_not_read"] == 0
    assert far.metadata["chart_ranges_not_read"] == 2
    assert far.metadata["chart_unresolved_series"] == 0
    assert "2 of this chart's cell ranges were not read: the workbook's chart read budget was reached." in far.text
    assert "May" not in far.text


def test_chart_reads_within_the_budget_read_every_range():
    near, far = _charts(_two_distant_uncached_charts())

    assert "| May | 9 |" in far.text and far.metadata["chart_ranges_not_read"] == 0


def _hide_frame(raw: bytes, *, grouped: bool = False) -> bytes:
    """Hide the first chart frame in Excel: itself, or the group it is moved into."""
    import re

    with zipfile.ZipFile(BytesIO(raw)) as archive:
        drawing = archive.read("xl/drawings/drawing1.xml").decode()
    frame = re.search(r"<graphicFrame>.*?</graphicFrame>", drawing, flags=re.DOTALL).group(0)
    if grouped:
        group = (
            '<grpSp><nvGrpSpPr><cNvPr id="7" name="Group" hidden="1"/><cNvGrpSpPr/></nvGrpSpPr><grpSpPr/>'
            f"<grpSp><nvGrpSpPr><cNvPr id='8' name='Inner'/><cNvGrpSpPr/></nvGrpSpPr><grpSpPr/>{frame}</grpSp></grpSp>"
        )
        drawing = drawing.replace(frame, group, 1)
    else:
        drawing = drawing.replace('name="Chart 1"/>', 'name="Chart 1" hidden="1"/>', 1)
    return _replace_parts(raw, {"xl/drawings/drawing1.xml": drawing})


@pytest.mark.parametrize("grouped", [False, True])
def test_a_chart_hidden_in_excel_is_withheld_without_hidden_data(grouped):
    raw = _hide_frame(_workbook_with_chart(_monthly_revenue(), charts=2), grouped=grouped)

    withheld = _charts(raw, mode="without_hidden_data")
    full = _charts(raw)

    # The visible sibling stays; the hidden one is not even parsed.
    assert len(withheld) == 1 and withheld[0].metadata["chart_hidden_in_excel"] is False
    assert len(full) == 2
    hidden = [chart for chart in full if chart.metadata["chart_hidden_in_excel"]]
    assert len(hidden) == 1 and "\nHidden in Excel.\n" in hidden[0].text
    assert hidden[0].metadata["chart_title"] == "Monthly revenue"


def test_worst_case_chart_metadata_fits_the_store_without_its_truncation():
    """300 series with 2,000-character names and sheet references, a huge title:
    capped at the source and flagged, so Milvus never cuts it at random."""
    import copy
    import json

    from services.storage.milvus_store import (
        DEFAULT_JSON_SAFETY_MARGIN_BYTES,
        MAX_DYNAMIC_FIELD_BYTES,
        _fit_milvus_row_limits,
    )

    name = "Évolution " * 200
    categories = ("Data!$A$2:$A$3", ["Q1", "Q2"])
    series = [
        _series(
            f"{name}{index}",
            categories,
            (f"'Feuille très longue {index}'!$B$2:$B$3", [index, index]),
            index=index,
            name_ref=f"'Feuille très longue {index}'!$B$1",
        )
        for index in range(300)
    ]
    (chart,) = _charts(_workbook_with_chart(_chart_xml([_plot("barChart", series)], title="Titre " * 2000)))
    metadata = chart.metadata

    assert metadata["chart_metadata_truncated"] is True
    assert metadata["chart_series_count"] == 300 and len(metadata["chart_series"]) == 64
    assert (
        len(json.dumps(metadata, ensure_ascii=False).encode())
        < MAX_DYNAMIC_FIELD_BYTES - DEFAULT_JSON_SAFETY_MARGIN_BYTES
    )
    entity = {"text": "chunk", "file_id": "f", "partition": "p", **copy.deepcopy(metadata)}
    _fit_milvus_row_limits(entity, static_fields={"text", "vector", "sparse", "_id", "partition", "file_id"})
    assert {key: entity[key] for key in metadata} == metadata


def test_a_long_title_never_pushes_the_chart_type_and_worksheet_out_of_a_chunk():
    from core.chunking.markdown_utils import chunk_xlsx_chart

    def words(text: str) -> int:
        return len(text.split())

    title = " ".join(f"word{index}" for index in range(300))
    xml = _chart_xml(
        [_plot("lineChart", [_series("S", ("Data!$A$2:$A$4", ["Jan", "Feb", "Mar"]), ("Data!$B$2:$B$4", [1, 2, 3]))])],
        title=title,
    )
    (chart,) = _charts(_workbook_with_chart(xml))
    pieces = chunk_xlsx_chart(chart.text, chart.metadata, 60, words)

    assert chart.metadata["chart_context"].startswith("Chart (line chart), worksheet Data: word0")
    assert len(pieces) > 1 and all(words(piece) <= 60 for piece in pieces)
    assert all(piece.startswith("Chart (line chart), worksheet Data: word0") for piece in pieces[1:])


def test_every_block_carries_the_workbook_chart_status_even_without_chart_chunks():
    """Every chart skipped: no chart chunk exists, but the table says so."""
    raw = _replace_parts(_workbook_with_chart(_monthly_revenue()), {"xl/charts/chart1.xml": "<c:chartSpace><broken>"})
    (table,) = _blocks(raw)

    assert table.metadata["xlsx_content_kind"] == "table"
    assert table.metadata["workbook_chart_extraction"] == "partial"
    assert table.metadata["workbook_chart_issue_count"] == 1
    assert "could not be read" in table.metadata["workbook_chart_issues"][0]


def test_a_failed_chart_extraction_is_reported_on_the_tables(monkeypatch):
    from core.indexing.parsers.tabular import xlsx_parser

    def failing(*args, **kwargs):
        raise ValueError("secret detail that must not reach metadata")

    monkeypatch.setattr(xlsx_parser, "WorkbookCharts", failing)
    (table,) = _blocks(_workbook_with_chart(_monthly_revenue()))

    assert table.metadata["workbook_chart_extraction"] == "failed"
    assert table.metadata["workbook_chart_issues"] == ["Chart extraction failed (ValueError)"]


def test_the_read_budget_is_a_workbook_chart_issue():
    blocks = _blocks(_two_distant_uncached_charts(), max_chart_scan_rows=10)

    assert {block.metadata["workbook_chart_extraction"] for block in blocks} == {"partial"}
    # The far chart's categories and values, and its value-format lookup.
    assert blocks[0].metadata["workbook_chart_issues"] == [
        "The chart read budget was reached: 3 cell ranges were not read"
    ]


def test_off_mode_reports_charts_as_off():
    (table,) = _blocks(_workbook_with_chart(_monthly_revenue()), mode="off")

    assert table.metadata["workbook_chart_extraction"] == "off"


def test_a_text_box_in_a_hidden_group_is_hidden_too():
    box = _text_box("Group note", grouped=True).replace("name='Group'/>", "name='Group' hidden='1'/>", 1)
    raw = _with_text_boxes(_workbook_with_chart(_monthly_revenue()), box, _text_box("Visible note"))

    (withheld,) = _text_boxes(_blocks(raw, mode="without_hidden_data"))
    (full,) = _text_boxes(_blocks(raw))

    assert "Group note" not in withheld.text and "Visible note" in withheld.text
    assert "(hidden in Excel) Group note" in full.text


# --- review round 1: limits after policy, list truncation, read priority -----


def test_hidden_charts_never_crowd_a_visible_one_out_of_the_chart_limit():
    """max_charts=1: the hidden chart comes first, the visible one must still fit."""
    raw = _hide_frame(_workbook_with_chart(_monthly_revenue(), charts=2))

    (chart,) = _charts(raw, mode="without_hidden_data", max_charts=1)

    assert chart.metadata["chart_hidden_in_excel"] is False
    assert chart.metadata["workbook_chart_extraction"] == "complete"


def test_withheld_text_boxes_never_crowd_a_visible_one_out_of_the_limit(monkeypatch):
    from core.indexing.parsers.tabular.xlsx_drawings import extract

    monkeypatch.setattr(extract, "_MAX_TEXT_BOXES", 1)
    monkeypatch.setattr(extract, "_TEXT_BOX_DISCOVERY", 4)
    workbook = Workbook()
    sheet = _data_sheet(workbook)
    sheet.row_dimensions[3].hidden = True
    _add_chart(sheet, sheet)
    raw = _with_text_boxes(
        _save(workbook),
        _text_box("Hidden note", hidden=True),
        _text_box("101.5", textlink="$B$3"),
        _text_box("Visible note"),
    )

    (box,) = _text_boxes(_blocks(raw, mode="without_hidden_data"))

    assert "Visible note" in box.text and "Hidden" not in box.text and "101.5" not in box.text


def test_a_capped_series_list_is_flagged_even_with_short_names():
    categories = ("Data!$A$2:$A$3", ["Q1", "Q2"])
    series = [_series(f"S{index}", categories, ("Data!$B$2:$B$3", [index, index]), index=index) for index in range(65)]
    (chart,) = _charts(_workbook_with_chart(_chart_xml([_plot("barChart", series)])))

    assert chart.metadata["chart_series_count"] == 65 and len(chart.metadata["chart_series"]) == 64
    assert chart.metadata["chart_metadata_truncated"] is True


def test_required_chart_values_are_read_before_optional_format_lookups():
    """The budget fits the values or the format lookups at row 2,000, not both:
    the values win, since a format only refines how they read."""
    near, far = _charts(_two_distant_uncached_charts(), max_chart_scan_rows=2010)

    assert "| May | 9 |" in far.text
    assert far.metadata["chart_ranges_not_read"] == 0
    assert near.metadata["workbook_chart_extraction"] == "partial"
