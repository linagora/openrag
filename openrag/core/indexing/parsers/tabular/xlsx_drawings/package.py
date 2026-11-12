"""Find the charts a workbook displays, in workbook order.

A chart lives in a drawing: a worksheet (or a chart sheet) has a ``drawing``
relationship to ``xl/drawings/drawingN.xml``, whose anchors place graphic
frames on the grid; a frame holding a chart has a ``chart`` relationship to
``xl/charts/chartN.xml``::

    xl/workbook.xml --sheet--> xl/worksheets/sheet1.xml
        --drawing--> xl/drawings/drawing1.xml   (anchor: from E2 to L20)
            --chart--> xl/charts/chart1.xml

Only visible sheets are walked, like the table extraction. Every part read
here is size-capped before it is parsed, and nothing in this module raises for
a malformed or oversized drawing: charts enrich a workbook, so a part that
cannot be read is skipped and reported, and the tables are still indexed.
"""

from __future__ import annotations

import zipfile
import zlib
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element, ParseError

from core.utils.exceptions import PipelineError
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from ..xlsx_validation import _related_part_path, _rels_path

_CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"
# Office 2016 chart types (waterfall, treemap, sunburst, histogram, funnel,
# box and whisker, map) use a different schema, ``cx:chartSpace`` (chartex.py).
_CHARTEX_URI = "http://schemas.microsoft.com/office/drawing/2014/chartex"
_ANCHORS = ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor")


@dataclass(frozen=True)
class ChartPlacement:
    """Where one chart is shown: its sheet, position on it, and chart part."""

    sheet_name: str
    # Workbook position of the sheet, the same numbering as ``SheetInfo.index``.
    sheet_index: int
    sheet_kind: str  # "worksheet" or "chartsheet"
    chart_path: str
    # One-based order of the chart on its sheet.
    position: int
    drawing_name: str | None = None
    # Cells the chart covers on a worksheet ("E2:L20"); None on a chart sheet.
    anchor: str | None = None
    # "chart" for classic DrawingML charts, "chartex" for Office 2016 ones.
    format: str = "chart"


@dataclass
class ChartInventory:
    placements: list[ChartPlacement] = field(default_factory=list)
    # Readable reasons for charts or drawings that were not read, one each.
    skipped: list[str] = field(default_factory=list)
    # Workbooks saved with the 1904 date system count date serials from 1904.
    date1904: bool = False
    # Defined names a series may plot (``=Revenue``): (sheet index or None for
    # workbook scope, folded name) -> reference text.
    defined_names: dict[tuple[int | None, str], str] = field(default_factory=dict)
    # Sheet name -> workbook position, for sheet-scoped names.
    sheet_indexes: dict[str, int] = field(default_factory=dict)


def _column_name(number: int) -> str:
    name = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _cell(marker: Element | None) -> str | None:
    """Turn a zero-based ``xdr:from`` / ``xdr:to`` marker into ``E2``."""
    if marker is None:
        return None
    column = marker.find("{*}col")
    row = marker.find("{*}row")
    try:
        return f"{_column_name(int(column.text) + 1)}{int(row.text) + 1}"
    except (AttributeError, TypeError, ValueError):
        return None


def _anchor_range(anchor: Element) -> str | None:
    start = _cell(anchor.find("{*}from"))
    end = _cell(anchor.find("{*}to"))
    if start and end and start != end:
        return f"{start}:{end}"
    return start


def _relationship_id(element: Element) -> str | None:
    return next((value for key, value in element.attrib.items() if key.endswith("}id")), None)


#: A package part that cannot be decompressed: a CRC mismatch, a corrupt
#: stream, a truncated member, an encrypted or unsupported compression method
#: (``NotImplementedError`` is a ``RuntimeError``). The part is skipped and the
#: other charts and every table are still indexed.
PART_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError)


class _Reader:
    def __init__(self, archive: zipfile.ZipFile, max_part_bytes: int, inventory: ChartInventory):
        self.archive = archive
        self.names = set(archive.namelist())
        self.max_part_bytes = max_part_bytes
        self.inventory = inventory

    def xml(self, name: str, what: str) -> Element | None:
        if name not in self.names:
            self.inventory.skipped.append(f"{what} {name} is missing from the workbook")
            return None
        if self.archive.getinfo(name).file_size > self.max_part_bytes:
            self.inventory.skipped.append(f"{what} {name} exceeds the {self.max_part_bytes}-byte limit")
            return None
        try:
            data = self.archive.read(name)
        except PART_READ_ERRORS as exc:
            self.inventory.skipped.append(f"{what} {name} could not be read ({type(exc).__name__})")
            return None
        try:
            return fromstring(data)
        except (ParseError, DefusedXmlException, ValueError) as exc:
            self.inventory.skipped.append(f"{what} {name} is not valid XML ({type(exc).__name__})")
            return None

    def relationships(self, part: str, *suffixes: str) -> dict[str, tuple[str, str]]:
        """Map relationship id to (type suffix, target part) for the given types."""
        rels = _rels_path(part)
        if rels not in self.names:
            return {}
        root = self.xml(rels, "Relationships")
        if root is None:
            return {}
        targets = {}
        for relationship in root:
            relationship_type = relationship.get("Type", "")
            suffix = next((suffix for suffix in suffixes if relationship_type.endswith(suffix)), None)
            if suffix is None or relationship.get("TargetMode") == "External":
                continue
            try:
                targets[relationship.get("Id", "")] = (suffix, _related_part_path(part, relationship.get("Target", "")))
            except PipelineError:
                self.inventory.skipped.append(f"A relationship of {part} has an invalid target")
        return targets


def locate_charts(archive: zipfile.ZipFile, *, max_charts: int, max_part_bytes: int) -> ChartInventory:
    """List the charts of every visible sheet, in workbook then drawing order."""
    inventory = ChartInventory()
    reader = _Reader(archive, max_part_bytes, inventory)
    workbook = reader.xml("xl/workbook.xml", "Workbook")
    if workbook is None:
        return inventory
    properties = workbook.find("{*}workbookPr")
    inventory.date1904 = properties is not None and properties.get("date1904", "0").lower() in {"1", "true"}
    for defined in workbook.findall("{*}definedNames/{*}definedName"):
        scope = defined.get("localSheetId")
        name = defined.get("name", "")
        if name and defined.text and (scope is None or scope.isdigit()):
            inventory.defined_names[(int(scope) if scope is not None else None, name.casefold())] = defined.text.strip()
    sheet_parts = reader.relationships("xl/workbook.xml", "/worksheet", "/chartsheet")
    sheets = workbook.findall("{*}sheets/{*}sheet")
    inventory.sheet_indexes = {sheet.get("name", ""): index for index, sheet in enumerate(sheets)}
    for index, sheet in enumerate(sheets):
        if sheet.get("state", "visible") != "visible":
            continue
        target = sheet_parts.get(_relationship_id(sheet) or "")
        if target is None:
            continue
        suffix, part = target
        kind = suffix.strip("/")
        position = 0
        for _, drawing in reader.relationships(part, "/drawing").values():
            root = reader.xml(drawing, "Drawing")
            if root is None:
                continue
            charts = reader.relationships(drawing, "/chart", "/chartEx")
            for anchor in (element for element in root if element.tag.rsplit("}", 1)[-1] in _ANCHORS):
                for frame in anchor.iterfind(".//{*}graphicFrame"):
                    data = frame.find("{*}graphic/{*}graphicData")
                    if data is None:
                        continue
                    if data.get("uri") not in {_CHART_URI, _CHARTEX_URI}:
                        continue
                    reference = data.find("{*}chart")
                    target = charts.get(_relationship_id(reference) or "") if reference is not None else None
                    if target is None:
                        continue
                    relationship, chart_path = target
                    chart_format = "chartex" if relationship == "/chartEx" else "chart"
                    if len(inventory.placements) >= max_charts:
                        inventory.skipped.append(f"Charts beyond the {max_charts}-chart limit were not indexed")
                        return inventory
                    position += 1
                    name = frame.find("{*}nvGraphicFramePr/{*}cNvPr")
                    inventory.placements.append(
                        ChartPlacement(
                            sheet_name=sheet.get("name", ""),
                            sheet_index=index,
                            sheet_kind=kind,
                            chart_path=chart_path,
                            position=position,
                            drawing_name=name.get("name") if name is not None else None,
                            anchor=_anchor_range(anchor) if kind == "worksheet" else None,
                            format=chart_format,
                        )
                    )
    return inventory
