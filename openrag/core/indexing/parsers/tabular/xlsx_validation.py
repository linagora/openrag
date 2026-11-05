"""Bound OOXML work before OpenPyXL allocates worksheet objects."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, fields
from xml.etree.ElementTree import ParseError

from core.utils.exceptions import ConfigError, PipelineError
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring, iterparse

MAIN_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
MACRO_MAIN_TYPE = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
TABLE_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
CELL_REF = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")
CELL_RANGE = re.compile(r"([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)\Z")
STREAMED_XML_PARTS = {"xl/sharedStrings.xml"}


@dataclass(frozen=True)
class XlsxLimits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_entries: int = 20_000
    max_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_entry_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: int = 200
    max_metadata_bytes: int = 8 * 1024 * 1024
    max_shared_strings_bytes: int = 32 * 1024 * 1024
    max_shared_strings: int = 250_000
    max_sheets: int = 128
    max_rows: int = 1_048_576
    max_columns: int = 4096
    max_cells: int = 5_000_000
    max_sheet_area: int = 5_000_000
    max_tables: int = 128
    max_merged_ranges: int = 100_000
    max_cell_chars: int = 32767
    max_batch_bytes: int = 2 * 1024 * 1024

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ConfigError(f"XLSX {field.name} must be a positive integer")


@dataclass(frozen=True)
class TableRegion:
    """A bounded, content-bearing table rectangle in a worksheet."""

    index: int
    start_row: int
    end_row: int
    start_column: int
    end_column: int
    header_row_hint: int | None = None


@dataclass(frozen=True)
class SheetInfo:
    name: str
    index: int
    path: str
    hidden: bool
    rows: int
    columns: int
    formula_cells: int
    missing_formula_values: int
    tables: tuple[TableRegion, ...]
    hidden_rows: frozenset[int]
    hidden_columns: frozenset[int]
    merged_ranges: tuple[tuple[int, int, int, int], ...]


def _column_runs(columns: set[int]):
    """Yield contiguous occupied-column ranges."""
    start = previous = None
    for column in sorted(columns):
        if start is None:
            start = previous = column
        elif column == previous + 1:
            previous = column
        else:
            yield start, previous
            start = previous = column
    if start is not None:
        yield start, previous


def _merge_preambles(
    regions: list[TableRegion],
    barriers: list[TableRegion] | None = None,
    typed_first_rows: set[tuple[int, int]] | None = None,
) -> list[TableRegion]:
    """Attach obvious title bands and isolated rows to their surrounding table.

    ``typed_first_rows`` holds the ``(start_row, start_column)`` of inferred
    regions whose first nonempty row contains a non-text value.
    """
    barriers = barriers or []
    typed_first_rows = typed_first_rows or set()
    merged: list[TableRegion] = []
    for region in regions:
        if merged:
            previous = merged[-1]
            previous_height = previous.end_row - previous.start_row + 1
            previous_width = previous.end_column - previous.start_column + 1
            region_height = region.end_row - region.start_row + 1
            region_width = region.end_column - region.start_column + 1
            is_preamble = previous_height == 1 or previous_width < region_width
            same_columns = previous.start_column == region.start_column and previous.end_column == region.end_column
            has_barrier = any(
                barrier.start_row <= region.start_row
                and previous.end_row <= barrier.end_row
                and barrier.start_column <= region.end_column
                and previous.start_column <= barrier.end_column
                for barrier in barriers
            )
            # A lone band after a table cannot itself contain both a header and
            # data. When it has the same columns, preserve it as a sparse
            # continuation of the preceding table even across a large blank
            # gap; otherwise it would be treated as a header-only table and
            # silently emit no row.
            # A header is text. A block with the same columns whose first row
            # holds a number, date or boolean is data resuming after a blank
            # spacer row; split off, that data row would become its header.
            typed_continuation = previous_height >= 2 and (region.start_row, region.start_column) in typed_first_rows
            if not has_barrier and same_columns and (region_height == 1 or typed_continuation):
                merged[-1] = TableRegion(
                    previous.index,
                    previous.start_row,
                    region.end_row,
                    previous.start_column,
                    previous.end_column,
                )
                continue
            if (
                not has_barrier
                and previous.end_row < region.start_row - 1
                and region.start_row - previous.end_row <= 32
                and region_height >= 2
                and region.start_column <= previous.start_column
                and previous.end_column <= region.end_column
                and is_preamble
            ):
                merged[-1] = TableRegion(
                    previous.index,
                    previous.start_row,
                    region.end_row,
                    region.start_column,
                    region.end_column,
                )
                continue
        merged.append(region)
    return [
        TableRegion(index, region.start_row, region.end_row, region.start_column, region.end_column)
        for index, region in enumerate(merged)
    ]


def _merged_header_depth(
    region: TableRegion,
    merged_columns_by_row: dict[int, list[tuple[int, int]]],
    populated_rows: set[int],
    hidden_rows: set[int],
) -> int:
    """Return merged header rows only when their leaf row contains cells.

    A worksheet title can be a merged cell immediately followed by a blank
    spacer and a regular table.  The blank spacer is not a header row, so it
    must not become a structural header hint merely because the title spans the
    table's columns.
    """
    expected_columns = set(range(region.start_column, region.end_column + 1))
    depth = 0
    for row_number in range(region.start_row, region.end_row):
        covered_columns: set[int] = set()
        for start_column, end_column in merged_columns_by_row.get(row_number, []):
            covered_columns.update(
                range(max(start_column, region.start_column), min(end_column, region.end_column) + 1)
            )
        if not expected_columns.issubset(covered_columns):
            break
        depth += 1
    leaf_row = region.start_row + depth
    return depth if leaf_row in populated_rows and leaf_row not in hidden_rows else 0


def invalid(message: str):
    return PipelineError(message, code="INVALID_XLSX", status_code=422)


def limit(message: str):
    return PipelineError(message, code="XLSX_LIMIT_EXCEEDED", status_code=413)


def _content_type(types, part_name: str) -> str | None:
    """Resolve a part's effective OPC content type."""
    override = next((node.get("ContentType") for node in types if node.get("PartName") == part_name), None)
    if override is not None:
        return override
    extension = part_name.rsplit(".", 1)[-1].lower() if "." in part_name else ""
    return next(
        (
            node.get("ContentType")
            for node in types
            if node.get("PartName") is None and node.get("Extension", "").lower() == extension
        ),
        None,
    )


def _related_part_path(source_path: str, target: str) -> str:
    """Resolve an internal OPC relationship without allowing package traversal."""
    path = posixpath.normpath(
        target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(source_path), target)
    )
    if not path.startswith("xl/"):
        raise invalid("XLSX relationship has an invalid target")
    return path


def _worksheet_table_regions(archive, types, worksheet_path: str, names: set[str], limits: XlsxLimits):
    """Return authoritative Excel Table rectangles keyed by worksheet relationship id."""
    rels_path = posixpath.join(
        posixpath.dirname(worksheet_path),
        "_rels",
        posixpath.basename(worksheet_path) + ".rels",
    )
    if rels_path not in names:
        return {}
    if archive.getinfo(rels_path).file_size > limits.max_metadata_bytes:
        raise limit("XLSX metadata exceeds the memory limit")
    relationships = fromstring(archive.read(rels_path))
    table_regions: dict[str, TableRegion] = {}
    for relationship in relationships:
        if not relationship.get("Type", "").endswith("/table"):
            continue
        rel_id = relationship.get("Id")
        if not rel_id or rel_id in table_regions:
            raise invalid("XLSX table relationships have missing or duplicate identifiers")
        if relationship.get("TargetMode") == "External":
            raise invalid("External table relationships are unsupported")
        table_path = _related_part_path(worksheet_path, relationship.get("Target", ""))
        if table_path not in names or _content_type(types, "/" + table_path) != TABLE_TYPE:
            raise invalid("XLSX table relationship has an invalid target")
        if archive.getinfo(table_path).file_size > limits.max_metadata_bytes:
            raise limit("XLSX metadata exceeds the memory limit")
        table = fromstring(archive.read(table_path))
        match = CELL_RANGE.fullmatch(table.get("ref", ""))
        if match is None:
            raise invalid("XLSX table has an invalid range")

        def column_number(label: str) -> int:
            number = 0
            for char in label:
                number = number * 26 + ord(char) - 64
            return number

        start_column = column_number(match[1])
        start_row = int(match[2])
        end_column = column_number(match[3])
        end_row = int(match[4])
        if (
            start_row > end_row
            or start_column > end_column
            or end_row > limits.max_rows
            or end_column > limits.max_columns
        ):
            raise invalid("XLSX table has an invalid range")
        try:
            header_row_count = int(table.get("headerRowCount", "1"))
        except ValueError as exc:
            raise invalid("XLSX table has an invalid header row count") from exc
        if header_row_count not in {0, 1}:
            raise invalid("XLSX table has an invalid header row count")
        region = TableRegion(
            len(table_regions),
            start_row,
            end_row,
            start_column,
            end_column,
            start_row if header_row_count else None,
        )
        for existing in table_regions.values():
            overlaps = not (
                region.end_row < existing.start_row
                or existing.end_row < region.start_row
                or region.end_column < existing.start_column
                or existing.end_column < region.start_column
            )
            if overlaps:
                raise invalid("XLSX tables contain overlapping ranges")
        table_regions[rel_id] = region
        if len(table_regions) > limits.max_tables:
            raise limit("XLSX table count exceeds the limit")
    return table_regions


def _combine_regions(
    inferred: list[TableRegion],
    explicit: list[TableRegion],
    merged_ranges: list[tuple[int, int, int, int]],
) -> list[TableRegion]:
    """Combine source-defined Excel Tables with independently inferred regions."""
    remaining = list(inferred)
    expanded: list[TableRegion] = []
    for table in explicit:
        title = next(
            (
                region
                for region in remaining
                if region.end_row < table.start_row
                and table.start_row - region.end_row <= 32
                and region.start_row == region.end_row
                and any(
                    start_row == end_row == region.start_row
                    and start_column == table.start_column
                    and end_column == table.end_column
                    for start_row, end_row, start_column, end_column in merged_ranges
                )
            ),
            None,
        )
        if title is not None:
            remaining.remove(title)
            table = TableRegion(
                table.index,
                title.start_row,
                table.end_row,
                table.start_column,
                table.end_column,
                table.header_row_hint,
            )
        expanded.append(table)
    combined = sorted(expanded + remaining, key=lambda region: (region.start_row, region.start_column))
    return [
        TableRegion(
            index,
            region.start_row,
            region.end_row,
            region.start_column,
            region.end_column,
            region.header_row_hint,
        )
        for index, region in enumerate(combined)
    ]


def _shared_string_count(archive, types, limits):
    """Validate the bounded shared-string table and return its entry count."""
    count = 0
    paths = [
        node.get("PartName", "").lstrip("/")
        for node in types
        if node.get("ContentType", "").endswith(".sharedStrings+xml")
    ]
    if len(paths) > 1:
        raise invalid("XLSX contains multiple shared string tables")
    for path in paths:
        if archive.getinfo(path).file_size > limits.max_shared_strings_bytes:
            raise limit("XLSX shared strings exceed the memory limit")
        with archive.open(path) as stream:
            stack = []
            for event, element in iterparse(stream, events=("start", "end")):
                if event == "start":
                    stack.append(element)
                    continue
                if element.tag.endswith("}si"):
                    texts = [node.text or "" for node in element.iter() if node.tag.endswith("}t")]
                    if sum(map(len, texts)) > limits.max_cell_chars:
                        raise limit("XLSX shared string exceeds the character limit")
                    count += 1
                    if count > limits.max_shared_strings:
                        raise limit("XLSX shared string count exceeds the memory limit")
                    if len(stack) > 1:
                        stack[-2].remove(element)
                    element.clear()
                stack.pop()
    return count


def _streamed_xml_parts(types) -> set[str]:
    """Return explicitly declared large XML parts consumed as streams."""
    suffixes = (".worksheet+xml", ".sharedStrings+xml")
    return STREAMED_XML_PARTS | {
        node.get("PartName", "").lstrip("/")
        for node in types
        if node.get("PartName") and node.get("ContentType", "").endswith(suffixes)
    }


def _is_eager_xml_part(name: str, streamed_parts: set[str]) -> bool:
    """Return whether OpenPyXL may materialize this XML package part."""
    return name.endswith((".xml", ".rels")) and name not in streamed_parts


def validate_ooxml(source, limits: XlsxLimits, *, allow_macros: bool = False) -> list[SheetInfo]:
    """Inspect actual cells, not untrusted dimension/count declarations.

    Retain at most one XML row. This is an intentional extra sequential read:
    it prevents malformed dimensions from driving excessive reader work.
    Shared strings/styles are capped separately because readers load them eagerly.
    """
    try:
        if hasattr(source, "seek"):
            source.seek(0, io.SEEK_END)
            size = source.tell()
            source.seek(0)
        else:
            size = source.stat().st_size
        if size > limits.max_archive_bytes:
            raise limit("XLSX archive exceeds the compressed-size limit")
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_entries:
                raise limit("XLSX archive contains too many entries")
            total = 0
            names = set()
            for entry in entries:
                name = entry.filename
                if name.startswith("/") or "\\" in name or ".." in name.split("/") or name in names:
                    raise invalid("XLSX archive contains an unsafe or duplicate entry path")
                if entry.flag_bits & 1:
                    raise invalid("Encrypted XLSX archives are unsupported")
                names.add(name)
                total += entry.file_size
                if entry.file_size > limits.max_entry_bytes or total > limits.max_uncompressed_bytes:
                    raise limit("XLSX archive exceeds the extraction limit")
                if entry.file_size / max(1, entry.compress_size) > limits.max_compression_ratio:
                    raise limit("XLSX archive exceeds the compression-ratio limit")
                if name == "xl/sharedStrings.xml" and entry.file_size > limits.max_shared_strings_bytes:
                    raise limit("XLSX shared strings exceed the memory limit")

            def xml(name):
                if archive.getinfo(name).file_size > limits.max_metadata_bytes:
                    raise limit("XLSX metadata exceeds the memory limit")
                return fromstring(archive.read(name))

            types = xml("[Content_Types].xml")
            workbook_type = _content_type(types, "/xl/workbook.xml")
            has_vba_project = any(name.lower().endswith("vbaproject.bin") for name in names)
            if workbook_type == MACRO_MAIN_TYPE:
                if not allow_macros:
                    raise invalid("Macro-enabled workbooks must use the .xlsm extension")
            elif workbook_type != MAIN_TYPE:
                raise invalid("OOXML package is not an XLSX workbook")
            elif has_vba_project:
                # A macro project hidden in a file presented as .xlsx is not
                # a plain workbook.  Require the explicit .xlsm route below.
                raise invalid("Macro-enabled workbooks must use the .xlsm extension")
            streamed_parts = _streamed_xml_parts(types)
            eager_entries = [entry for entry in entries if _is_eager_xml_part(entry.filename, streamed_parts)]
            eager_metadata_bytes = sum(entry.file_size for entry in eager_entries)
            if any(entry.file_size > limits.max_metadata_bytes for entry in eager_entries):
                raise limit("XLSX metadata exceeds the memory limit")
            for part in types:
                if part.get("ContentType", "").endswith(".styles+xml"):
                    if archive.getinfo(part.get("PartName", "").lstrip("/")).file_size > limits.max_metadata_bytes:
                        raise limit("XLSX styles exceed the memory limit")
            shared_count = _shared_string_count(archive, types, limits)
            relationship_nodes = list(xml("xl/_rels/workbook.xml.rels"))
            relationship_ids = [relationship.get("Id") for relationship in relationship_nodes]
            if any(not rel_id for rel_id in relationship_ids) or len(set(relationship_ids)) != len(relationship_ids):
                raise invalid("XLSX workbook relationships have missing or duplicate identifiers")
            relationships = dict(zip(relationship_ids, relationship_nodes, strict=True))
            # Read-only OpenPyXL still materializes chart sheets and their
            # drawing media while opening a workbook. Bound those binary parts
            # before the backend can allocate them; ordinary worksheet media
            # remains ignored and is governed by the archive limits instead.
            if any(relationship.get("Type", "").endswith("/chartsheet") for relationship in relationship_nodes):
                eager_metadata_bytes += sum(
                    entry.file_size for entry in entries if entry.filename.startswith("xl/media/")
                )
            if eager_metadata_bytes > limits.max_metadata_bytes:
                raise limit("XLSX metadata exceeds the memory limit")
            sheets = xml("xl/workbook.xml").findall("{*}sheets/{*}sheet")
            if len(sheets) > limits.max_sheets:
                raise limit("XLSX worksheet count exceeds the limit")
            infos = []
            sheet_names = [sheet.get("name", "") for sheet in sheets]
            folded_sheet_names = [name.casefold() for name in sheet_names]
            if any(not name for name in sheet_names) or len(set(folded_sheet_names)) != len(folded_sheet_names):
                raise invalid("XLSX sheet names are missing or duplicated")
            if any(sheet.get("state", "visible") not in {"visible", "hidden", "veryHidden"} for sheet in sheets):
                raise invalid("XLSX worksheet has an invalid visibility state")
            cells_seen = 0
            tables_seen = 0
            merged_ranges_seen = 0
            worksheet_paths = set()
            for index, sheet in enumerate(sheets):
                rel_id = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), None)
                relationship = relationships[rel_id]
                if relationship.get("TargetMode") == "External":
                    raise invalid("External worksheet relationships are unsupported")
                if not relationship.get("Type", "").endswith("/worksheet"):
                    continue
                target = relationship.get("Target", "")
                path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                if not path.startswith("xl/") or path not in names:
                    raise invalid("XLSX worksheet relationship has an invalid target")
                if path in worksheet_paths:
                    raise invalid("XLSX worksheet relationships contain a duplicate target")
                worksheet_paths.add(path)
                table_relationships = _worksheet_table_regions(archive, types, path, names, limits)
                explicit_regions = list(table_relationships.values())
                table_part_ids: list[str] = []
                table_events: list[tuple[int, int, TableRegion]] = []
                for region in explicit_regions:
                    table_events.append((region.start_row, 1, region))
                    table_events.append((region.end_row + 1, -1, region))
                table_events.sort(key=lambda event: (event[0], event[1]))
                table_event_index = 0
                active_tables: set[TableRegion] = set()
                active_column_starts: list[int] = []
                active_column_ends: list[int] = []
                max_row = max_col = 0
                visible_formula_columns: dict[int, int] = {}
                missing_formula_columns: dict[int, int] = {}
                previous_row = current_row = previous_col = 0
                current_cell_column = 0
                row_content_columns: set[int] = set()
                populated_rows: set[int] = set()
                band_start = band_end = None
                band_columns: set[int] = set()
                # Per band column: (first content row, whether that cell is non-text).
                band_first_cells: dict[int, tuple[int, bool]] = {}
                row_typed_columns: set[int] = set()
                typed_first_rows: set[tuple[int, int]] = set()
                regions: list[TableRegion] = []
                merged_ranges: list[tuple[int, int, int, int]] = []
                merged_columns_by_row: dict[int, list[tuple[int, int]]] = {}
                hidden_rows: set[int] = set()
                hidden_columns: set[int] = set()

                def advance_explicit_tables(row_number: int):
                    nonlocal table_event_index, active_column_starts, active_column_ends
                    changed = False
                    while table_event_index < len(table_events) and table_events[table_event_index][0] <= row_number:
                        _, operation, region = table_events[table_event_index]
                        if operation == 1:
                            active_tables.add(region)
                        else:
                            active_tables.discard(region)
                        table_event_index += 1
                        changed = True
                    if changed:
                        intervals = sorted((region.start_column, region.end_column) for region in active_tables)
                        active_column_starts = [start for start, _ in intervals]
                        active_column_ends = [end for _, end in intervals]

                def is_in_explicit_table(column: int) -> bool:
                    # At most max_tables intervals exist. Binary search avoids
                    # multiplying large worksheet cell counts by that bound.
                    low, high = 0, len(active_column_starts)
                    while low < high:
                        middle = (low + high) // 2
                        if active_column_starts[middle] <= column:
                            low = middle + 1
                        else:
                            high = middle
                    interval = low - 1
                    return interval >= 0 and column <= active_column_ends[interval]

                def finish_band():
                    nonlocal band_start, band_end, band_columns, band_first_cells
                    if band_start is not None and band_end is not None:
                        for start_column, end_column in _column_runs(band_columns):
                            regions.append(TableRegion(len(regions), band_start, band_end, start_column, end_column))
                            firsts = [band_first_cells[column] for column in range(start_column, end_column + 1)]
                            first_row = min(row for row, _ in firsts)
                            if any(typed for row, typed in firsts if row == first_row):
                                typed_first_rows.add((band_start, start_column))
                    band_start = band_end = None
                    band_columns = set()
                    band_first_cells = {}

                # clear completed children so memory does not grow with row count.
                with archive.open(path) as stream:
                    stack = []
                    for event, element in iterparse(stream, events=("start", "end")):
                        if event == "start":
                            stack.append(element)
                            if element.tag.endswith("}row"):
                                current_row = int(element.get("r", "0"))
                                if current_row <= previous_row:
                                    raise invalid("XLSX row coordinates are missing or out of order")
                                if current_row > limits.max_rows:
                                    raise limit("XLSX row coordinate exceeds the row limit")
                                previous_row, previous_col = current_row, 0
                                advance_explicit_tables(current_row)
                                row_content_columns = set()
                                row_typed_columns = set()
                                if element.get("hidden", "0").lower() in {"1", "true"}:
                                    hidden_rows.add(current_row)
                            if element.tag.endswith("}c"):
                                match = CELL_REF.fullmatch(element.get("r", ""))
                                if match is None:
                                    raise invalid("XLSX cell is missing a valid coordinate")
                                col = 0
                                for char in match[1]:
                                    col = col * 26 + ord(char) - 64
                                row = int(match[2])
                                if row > limits.max_rows or col > limits.max_columns:
                                    raise limit("XLSX cell coordinate exceeds row/column limits")
                                if row != current_row or col <= previous_col:
                                    raise invalid("XLSX cell coordinates are inconsistent or out of order")
                                previous_col = col
                                current_cell_column = col
                                cells_seen += 1
                                if cells_seen > limits.max_cells:
                                    raise limit("XLSX cell count exceeds the limit")
                                max_row, max_col = max(max_row, row), max(max_col, col)
                            continue
                        tag = element.tag.rsplit("}", 1)[-1]
                        if tag == "col" and element.get("hidden", "0").lower() in {"1", "true"}:
                            start_column = int(element.get("min", "0"))
                            end_column = int(element.get("max", "0"))
                            if start_column < 1 or end_column < start_column or end_column > 16_384:
                                raise invalid("XLSX hidden column range is invalid")
                            hidden_columns.update(range(start_column, min(end_column, limits.max_columns) + 1))
                        if tag == "c":
                            value = element.find("{*}v")
                            formula = element.find("{*}f")
                            if formula is not None and current_row not in hidden_rows:
                                visible_formula_columns[current_cell_column] = (
                                    visible_formula_columns.get(current_cell_column, 0) + 1
                                )
                                if value is None or not value.text:
                                    missing_formula_columns[current_cell_column] = (
                                        missing_formula_columns.get(current_cell_column, 0) + 1
                                    )
                            if sum(len(t.text or "") for t in element.iter()) > limits.max_cell_chars:
                                raise limit("XLSX cell text exceeds the character limit")
                            if element.get("t") == "s":
                                string_index = int(value.text) if value is not None and value.text else -1
                                if not 0 <= string_index < shared_count:
                                    raise invalid("XLSX cell references a missing shared string")
                            inline_text = any(node.text for node in element.iter() if node.tag.endswith("}t"))
                            if (
                                formula is not None or (value is not None and value.text is not None) or inline_text
                            ) and not is_in_explicit_table(current_cell_column):
                                row_content_columns.add(current_cell_column)
                                if element.get("t") not in {"s", "str", "inlineStr"}:
                                    row_typed_columns.add(current_cell_column)
                        if tag == "row":
                            if row_content_columns:
                                populated_rows.add(current_row)
                                if band_start is None:
                                    band_start = current_row
                                elif current_row > band_end + 1:
                                    finish_band()
                                    band_start = current_row
                                band_end = current_row
                                band_columns.update(row_content_columns)
                                for column in row_content_columns:
                                    band_first_cells.setdefault(column, (current_row, column in row_typed_columns))
                            else:
                                finish_band()
                        if tag == "mergeCell":
                            match = CELL_RANGE.fullmatch(element.get("ref", ""))
                            if match is None:
                                raise invalid("XLSX merged cell has an invalid range")

                            def column_number(label: str) -> int:
                                number = 0
                                for char in label:
                                    number = number * 26 + ord(char) - 64
                                return number

                            start_column = column_number(match[1])
                            start_row = int(match[2])
                            end_column = column_number(match[3])
                            end_row = int(match[4])
                            if (
                                start_row > end_row
                                or start_column > end_column
                                or end_row > limits.max_rows
                                or end_column > limits.max_columns
                            ):
                                raise invalid("XLSX merged cell has an invalid range")
                            if merged_ranges_seen >= limits.max_merged_ranges:
                                raise limit("XLSX merged-cell count exceeds the limit")
                            merged_ranges_seen += 1
                            merged_ranges.append((start_row, end_row, start_column, end_column))
                            if start_row == end_row and end_column > start_column:
                                merged_columns_by_row.setdefault(start_row, []).append((start_column, end_column))
                        if tag == "tablePart":
                            rel_id = next((value for key, value in element.attrib.items() if key.endswith("}id")), None)
                            if not rel_id:
                                raise invalid("XLSX table part is missing a relationship identifier")
                            table_part_ids.append(rel_id)
                        if tag in {"c", "row"} or len(stack) <= 2:
                            if len(stack) > 1:
                                stack[-2].remove(element)
                            element.clear()
                        stack.pop()
                finish_band()
                if len(set(table_part_ids)) != len(table_part_ids) or set(table_part_ids) != set(table_relationships):
                    raise invalid("XLSX table parts do not match their relationships")
                formulas = sum(
                    count for column, count in visible_formula_columns.items() if column not in hidden_columns
                )
                missing = sum(
                    count for column, count in missing_formula_columns.items() if column not in hidden_columns
                )
                regions = _combine_regions(
                    _merge_preambles(regions, explicit_regions, typed_first_rows),
                    explicit_regions,
                    merged_ranges,
                )
                hinted_regions: list[TableRegion] = []
                for region in regions:
                    header_depth = (
                        _merged_header_depth(region, merged_columns_by_row, populated_rows, hidden_rows)
                        if region.header_row_hint is None
                        else 0
                    )
                    hinted_regions.append(
                        TableRegion(
                            region.index,
                            region.start_row,
                            region.end_row,
                            region.start_column,
                            region.end_column,
                            region.header_row_hint or (region.start_row + header_depth if header_depth else None),
                        )
                    )
                regions = hinted_regions
                tables_seen += len(regions)
                if tables_seen > limits.max_tables:
                    raise limit("XLSX table count exceeds the limit")
                if max_row * max_col > limits.max_sheet_area:
                    raise limit("XLSX worksheet rectangle exceeds the traversal limit")
                infos.append(
                    SheetInfo(
                        sheet.get("name", ""),
                        index,
                        path,
                        sheet.get("state", "visible") != "visible",
                        max_row,
                        max_col,
                        formulas,
                        missing,
                        tuple(regions),
                        frozenset(hidden_rows),
                        frozenset(hidden_columns),
                        tuple(merged_ranges),
                    )
                )
            return infos
    except (zipfile.BadZipFile, KeyError, ValueError, ParseError, DefusedXmlException, RuntimeError) as exc:
        raise invalid("File is not a valid XLSX workbook") from exc
    finally:
        if hasattr(source, "seek"):
            source.seek(0)
