"""Bound OOXML work before OpenPyXL allocates worksheet objects."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from bisect import bisect_right
from dataclasses import dataclass, fields, replace
from xml.etree.ElementTree import ParseError

from core.utils.exceptions import ConfigError, PipelineError
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring, iterparse

MAIN_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
MACRO_MAIN_TYPE = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
TABLE_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
# The Excel grid; coordinates beyond it are malformed, not merely large.
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_COLUMNS = 16_384
CELL_REF = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")
CELL_RANGE = re.compile(r"([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)\Z")


@dataclass(frozen=True)
class XlsxLimits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_entries: int = 20_000
    max_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_entry_bytes: int = 256 * 1024 * 1024
    max_compression_ratio: int = 200
    max_metadata_bytes: int = 8 * 1024 * 1024
    max_shared_strings_bytes: int = 32 * 1024 * 1024
    # OpenPyXL keeps the whole shared-string table in memory, about 4.7x the
    # size of its XML (measured with ~20-character strings). The byte limit
    # above is therefore the memory guard: it caps the table at roughly 150 MB
    # whatever the entry count. This count only bounds per-entry overhead and
    # must not reject ordinary exports first: 60k rows x 5 columns of unique
    # text is 300k entries but only ~11 MB of XML.
    max_shared_strings: int = 1_000_000
    max_sheets: int = 128
    max_rows: int = 1_048_576
    max_columns: int = 4096
    max_cells: int = 5_000_000
    max_sheet_area: int = 5_000_000
    max_tables: int = 256
    max_merged_ranges: int = 100_000
    max_cell_chars: int = 32767
    max_batch_bytes: int = 2 * 1024 * 1024
    # Temporary-file space per worksheet for rows of tables waiting for their
    # turn in the single shared scan (see xlsx_row_router). Past it, waiting
    # tables fall back to their own scan: slower, never more disk.
    max_spool_bytes: int = 256 * 1024 * 1024
    # Part of max_spool_bytes kept in memory before the temporary file is used.
    # Typical side-by-side sheets fit, so they never depend on a usable TMPDIR.
    max_spool_memory_bytes: int = 32 * 1024 * 1024
    # Charts read across the workbook; later ones are skipped, never rejected.
    max_charts: int = 256
    # Size of one drawing or chart part, read whole when the workbook is parsed.
    # Cached values make charts large: 10 series x 5,000 points is about 4 MB.
    max_chart_part_bytes: int = 16 * 1024 * 1024
    # Chart parts read across the workbook (about 2-3 s of parsing at most);
    # later charts are skipped, never rejected.
    max_chart_bytes: int = 64 * 1024 * 1024
    # Data points rendered per chart; the chunk says when points were left out.
    max_chart_points: int = 5_000
    # A chart over data already indexed as worksheet tables repeats at most
    # this many points; past it, it keeps a summary and points to the tables.
    max_chart_points_from_tables: int = 200
    # Worksheet rows the chart cell reads may parse across the workbook. Each
    # read parses its sheet from the first row (about 14 us a row), so this
    # bounds their time: about 30 s at most, two full passes of a 1M-row sheet.
    max_chart_scan_rows: int = 2_000_000

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
    # Blank columns inside the span that only separate parts of one table.
    spacer_columns: frozenset[int] = frozenset()
    # An Excel Table's declared tableColumns names, one per span column, and its
    # first data row (a merged title absorbed above it is context, not data).
    # They label the table when it has no header row or its header is unusable.
    declared_column_names: tuple[str, ...] | None = None
    data_start_row: int | None = None
    # Data rows covered by an Excel Table's own AutoFilter (first, last).
    filter_rows: tuple[int, int] | None = None

    @property
    def occupied_columns(self) -> set[int]:
        return set(range(self.start_column, self.end_column + 1)) - self.spacer_columns


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
    uncached_string_formulas: frozenset[tuple[int, int]] = frozenset()
    # Rows hidden by an active AutoFilter. They are extracted: a filter is a
    # view of the data, not a decision to exclude rows from it.
    filtered_rows: frozenset[int] = frozenset()
    # Number of separate blocks detected when a worksheet had more than
    # ``max_tables`` of them and blocks sharing columns were merged; 0 otherwise.
    merged_table_blocks: int = 0


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


def _join_spacer_columns(
    runs: list[tuple[int, int]],
    extents: dict[int, tuple[int, int]],
    band_start: int,
    band_end: int,
    barriers: list[TableRegion],
) -> list[tuple[int, int, frozenset[int]]]:
    """Rejoin column runs that one blank column splits out of a single table.

    A spacer column between two halves of a table would otherwise cut every
    row in two, losing the link between its values. Runs covering exactly the
    same rows with one blank column between them are kept together. Wrongly
    merging only widens a table, while wrongly splitting loses associations;
    truly separate adjacent tables can be declared as Excel Tables, which act
    as barriers here.
    """

    def rows_of(start: int, end: int) -> tuple[int, int]:
        columns = [extents[column] for column in range(start, end + 1) if column in extents]
        return min(first for first, _ in columns), max(last for _, last in columns)

    joined: list[tuple[int, int, frozenset[int]]] = []
    for start, end in runs:
        if joined:
            previous_start, previous_end, spacers = joined[-1]
            gap = previous_end + 1
            blocked = any(
                barrier.start_column <= gap <= barrier.end_column
                and barrier.start_row <= band_end
                and band_start <= barrier.end_row
                for barrier in barriers
            )
            if (
                start == previous_end + 2
                and not blocked
                and rows_of(previous_start, previous_end) == rows_of(start, end)
            ):
                joined[-1] = (previous_start, end, spacers | {gap})
                continue
        joined.append((start, end, frozenset()))
    return joined


def _merge_preambles(
    regions: list[TableRegion],
    barriers: list[TableRegion] | None = None,
    typed_first_rows: set[tuple[int, int]] | None = None,
) -> list[TableRegion]:
    """Attach obvious title bands and isolated rows to their surrounding table.

    ``typed_first_rows`` holds the ``(start_row, start_column)`` of inferred
    regions whose first nonempty row contains a non-text value.
    """
    merged: list[TableRegion] = []
    for region in regions:
        _fold_preamble(merged, region, barriers or [], typed_first_rows or set())
    return _reindexed_regions(merged)


def _reindexed_regions(merged: list[TableRegion]) -> list[TableRegion]:
    """Number regions in order and drop header hints that merging may have invalidated."""
    return [replace(region, index=index, header_row_hint=None) for index, region in enumerate(merged)]


def _fold_preamble(
    merged: list[TableRegion],
    region: TableRegion,
    barriers: list[TableRegion],
    typed_first_rows: set[tuple[int, int]],
) -> None:
    """Fold one inferred region into ``merged`` (one step of ``_merge_preambles``).

    Each region is compared with the current last merged region only, so
    folding regions as the worksheet scan produces them gives exactly the
    batch result while keeping the retained list bounded by the merged count.
    """
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
        spacers = frozenset(
            (previous.spacer_columns | region.spacer_columns) - (previous.occupied_columns | region.occupied_columns)
        )
        if not has_barrier and same_columns and (region_height == 1 or typed_continuation):
            merged[-1] = replace(previous, end_row=region.end_row, spacer_columns=spacers)
            return
        if (
            not has_barrier
            and previous.end_row < region.start_row - 1
            and region.start_row - previous.end_row <= 32
            and region_height >= 2
            and region.start_column <= previous.start_column
            and previous.end_column <= region.end_column
            and is_preamble
        ):
            merged[-1] = replace(
                previous,
                end_row=region.end_row,
                start_column=region.start_column,
                end_column=region.end_column,
                spacer_columns=spacers,
            )
            return
    merged.append(region)


# Merged column-header hierarchies deeper than this are not headers; the bound
# also caps how far the parser looks ahead to validate a structural header row.
MAX_MERGED_HEADER_DEPTH = 32


def _merged_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of inclusive column intervals, sorted and disjoint."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _merged_header_depth(
    region: TableRegion,
    merged_columns_by_row: dict[int, list[tuple[int, int]]],
    populated_rows: set[int],
    hidden_rows: set[int],
    coverage_cache: dict[int, list[tuple[int, int]]] | None = None,
) -> int:
    """Return merged header rows only when their leaf row contains cells.

    A worksheet title can be a merged cell immediately followed by a blank
    spacer and a regular table.  The blank spacer is not a header row, so it
    must not become a structural header hint merely because the title spans the
    table's columns. Coverage is tested on disjoint intervals, never by
    expanding merges into single columns, and each row's union is computed once.
    """
    coverage_cache = {} if coverage_cache is None else coverage_cache
    occupied_runs = list(_column_runs(region.occupied_columns))
    depth = 0
    for row_number in range(region.start_row, min(region.end_row, region.start_row + MAX_MERGED_HEADER_DEPTH)):
        if row_number not in coverage_cache:
            coverage_cache[row_number] = _merged_intervals(merged_columns_by_row.get(row_number, []))
        covered = coverage_cache[row_number]
        if not all(
            (position := bisect_right(covered, (start, EXCEL_MAX_COLUMNS + 1)) - 1) >= 0 and covered[position][1] >= end
            for start, end in occupied_runs
        ):
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
            or end_row > EXCEL_MAX_ROWS
            or end_column > EXCEL_MAX_COLUMNS
        ):
            raise invalid("XLSX table has an invalid range")
        if end_row > limits.max_rows or end_column > limits.max_columns:
            raise limit("XLSX table coordinate exceeds row/column limits")
        # Explicit table references can extend far beyond populated cells.
        # Read-only OpenPyXL iterates through the declared rectangle, so apply
        # the same worksheet traversal bound before opening the workbook.
        if end_row * end_column > limits.max_sheet_area:
            raise limit("XLSX worksheet rectangle exceeds the traversal limit")
        try:
            header_row_count = int(table.get("headerRowCount", "1"))
        except ValueError as exc:
            raise invalid("XLSX table has an invalid header row count") from exc
        if header_row_count not in {0, 1}:
            raise invalid("XLSX table has an invalid header row count")
        declared = [str(column.get("name") or "") for column in table.findall("{*}tableColumns/{*}tableColumn")]
        # A mismatched list cannot be mapped to columns; generate names.
        width = end_column - start_column + 1
        column_names = tuple(declared) if len(declared) == width else ("",) * width
        region = TableRegion(
            len(table_regions),
            start_row,
            end_row,
            start_column,
            end_column,
            start_row if header_row_count else None,
            declared_column_names=column_names,
            data_start_row=start_row + header_row_count,
            filter_rows=_auto_filter_rows(table.find("{*}autoFilter")),
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


def _auto_filter_rows(auto_filter) -> tuple[int, int] | None:
    """Data rows (first, last) of an ``<autoFilter ref>``: the first row holds the
    filter buttons, i.e. the headers. ``None`` when absent or unusable."""
    if auto_filter is None:
        return None
    match = CELL_RANGE.fullmatch(auto_filter.get("ref", ""))
    if match is None:
        return None
    first, last = int(match[2]), int(match[4])
    return (first + 1, last) if first < last else None


def _rows_covered(rows: set[int], ranges: list[tuple[int, int]]) -> set[int]:
    """The subset of ``rows`` inside any of the inclusive ``ranges``."""
    covered: set[int] = set()
    if not rows or not ranges:
        return covered
    intervals: list[tuple[int, int]] = []
    for first, last in sorted(ranges):
        if intervals and first <= intervals[-1][1] + 1:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], last))
        else:
            intervals.append((first, last))
    starts = [first for first, _ in intervals]
    for row in rows:
        position = bisect_right(starts, row) - 1
        if position >= 0 and row <= intervals[position][1]:
            covered.add(row)
    return covered


def _rectangles_intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """Whether two (start_row, end_row, start_column, end_column) rectangles share a cell."""
    return not (a[1] < b[0] or b[1] < a[0] or a[3] < b[2] or b[3] < a[2])


def _bounds(region: TableRegion) -> tuple[int, int, int, int]:
    """The region's (start_row, end_row, start_column, end_column) rectangle."""
    return region.start_row, region.end_row, region.start_column, region.end_column


def _fold_column_band(merged: list[TableRegion], region: TableRegion, barriers: list[TableRegion]) -> None:
    """Fold a block into ``merged`` once a worksheet has more blocks than ``max_tables``.

    The block joins the most recent table that shares any of its columns, so
    repeated blocks stacked in the same columns (a block per week, per store...)
    become one table whose later header rows are indexed as data rows. It stays
    separate when the union would cross an Excel Table or overlap another table.
    Blocks arrive in row order, so the union never reaches back over a block
    that is not part of it.
    """
    target_index = None
    for index, table in enumerate(merged):
        if table.start_column <= region.end_column and region.start_column <= table.end_column:
            if target_index is None or table.end_row > merged[target_index].end_row:
                target_index = index
    if target_index is not None:
        target = merged[target_index]
        union = (
            min(target.start_row, region.start_row),
            max(target.end_row, region.end_row),
            min(target.start_column, region.start_column),
            max(target.end_column, region.end_column),
        )
        crosses_table = any(_rectangles_intersect(union, _bounds(barrier)) for barrier in barriers)
        overlaps_other = any(
            index != target_index and _rectangles_intersect(union, _bounds(table)) for index, table in enumerate(merged)
        )
        if not crosses_table and not overlaps_other:
            occupied = target.occupied_columns | region.occupied_columns
            merged[target_index] = replace(
                target,
                start_row=union[0],
                end_row=union[1],
                start_column=union[2],
                end_column=union[3],
                spacer_columns=frozenset(set(range(union[2], union[3] + 1)) - occupied),
            )
            return
    merged.append(region)


def _combine_regions(
    inferred: list[TableRegion],
    explicit: list[TableRegion],
    merged_ranges: list[tuple[int, int, int, int]],
) -> list[TableRegion]:
    """Combine source-defined Excel Tables with independently inferred regions."""
    remaining = list(inferred)
    expanded: list[TableRegion] = []
    # One-row merges by (row, first column, last column): an O(1) title lookup
    # instead of scanning every merged range for every candidate row.
    single_row_merges = {
        (start_row, start_column, end_column)
        for start_row, end_row, start_column, end_column in merged_ranges
        if start_row == end_row
    }
    for table in explicit:
        title = next(
            (
                region
                for region in remaining
                if region.end_row < table.start_row
                and table.start_row - region.end_row <= 32
                and region.start_row == region.end_row
                and region.start_column == table.start_column
                and region.end_column <= table.end_column
                and (region.start_row, table.start_column, table.end_column) in single_row_merges
            ),
            None,
        )
        if title is not None:
            remaining.remove(title)
            table = replace(table, start_row=title.start_row)
        expanded.append(table)
    combined = sorted(expanded + remaining, key=lambda region: (region.start_row, region.start_column))
    return [replace(region, index=index) for index, region in enumerate(combined)]


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
            string_chars = 0
            for event, element in iterparse(stream, events=("start", "end")):
                if event == "start":
                    stack.append(element)
                    if element.tag.endswith("}si"):
                        string_chars = 0
                    continue
                if element.tag.endswith("}t"):
                    string_chars += len(element.text or "")
                    if string_chars > limits.max_cell_chars:
                        raise limit("XLSX shared string exceeds the character limit")
                if element.tag.endswith("}si"):
                    count += 1
                    if count > limits.max_shared_strings:
                        raise limit("XLSX shared string count exceeds the memory limit")
                if len(stack) > 1:
                    stack[-2].remove(element)
                element.clear()
                stack.pop()
    return count


# Parts read-only OpenPyXL reads whole while opening any workbook, at fixed
# paths (openpyxl.reader.excel.ExcelReader.read, apply_stylesheet).
READER_FIXED_PARTS = (
    "[Content_Types].xml",
    "docProps/core.xml",
    "docProps/custom.xml",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
    "xl/styles.xml",
    "xl/theme/theme1.xml",
)


def _rels_path(part: str) -> str:
    directory, name = posixpath.split(part)
    return posixpath.join(directory, "_rels", f"{name}.rels")


def _reader_loaded_parts(archive, names: set[str], relationship_nodes, limits: XlsxLimits) -> set[str]:
    """Return the parts read-only OpenPyXL materializes while opening the workbook.

    Worksheets and shared strings are streamed. Everything else a worksheet
    references (comments, drawings, media, pivot tables and caches, external
    links with ``keep_links=False``) is never opened in read-only mode, so it
    is governed by the per-entry and total extraction limits instead. The
    reader still parses each sheet's relationship part, and loads a chart sheet
    whole together with what it references: drawings, charts and images.
    """
    loaded = {name for name in READER_FIXED_PARTS if name in names}
    chartsheets = []
    for relationship in relationship_nodes:
        relationship_type = relationship.get("Type", "")
        if relationship.get("TargetMode") == "External" or not relationship_type.endswith(
            ("/worksheet", "/chartsheet")
        ):
            continue
        target = _related_part_path("xl/workbook.xml", relationship.get("Target", ""))
        if _rels_path(target) in names:
            loaded.add(_rels_path(target))
        if relationship_type.endswith("/chartsheet") and target in names:
            chartsheets.append(target)
    total = sum(archive.getinfo(name).file_size for name in loaded)

    def add(name: str) -> None:
        # Bound the running total before any further relationship part is read.
        nonlocal total
        if name not in loaded:
            loaded.add(name)
            total += archive.getinfo(name).file_size
            if total > limits.max_metadata_bytes:
                raise limit("XLSX metadata exceeds the memory limit")

    if total > limits.max_metadata_bytes:
        raise limit("XLSX metadata exceeds the memory limit")
    visited: set[str] = set()
    while chartsheets:
        part = chartsheets.pop()
        if part in visited:
            continue
        visited.add(part)
        add(part)
        rels = _rels_path(part)
        if rels not in names:
            continue
        add(rels)
        for relationship in fromstring(archive.read(rels)):
            if relationship.get("TargetMode") == "External":
                continue
            try:
                target = _related_part_path(part, relationship.get("Target", ""))
            except PipelineError:
                continue
            if target in names:
                chartsheets.append(target)
    return loaded


def validate_ooxml(source, limits: XlsxLimits, *, allow_macros: bool = False) -> list[SheetInfo]:
    """Inspect actual cells, not untrusted dimension/count declarations.

    Retain only the open element path: every completed element is released, and
    a cell's children are summarized as they complete. This is an intentional
    extra sequential read: it prevents malformed dimensions from driving
    excessive reader work.
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
            workbook_types = {
                MAIN_TYPE,
                MACRO_MAIN_TYPE,
                "application/vnd.ms-excel.template.macroEnabled.main+xml",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
            }
            workbook_parts = [
                part.get("PartName")
                for part in types
                if part.get("PartName") is not None and part.get("ContentType") in workbook_types
            ]
            if workbook_parts not in ([], ["/xl/workbook.xml"]):
                raise invalid("XLSX contains competing or invalid workbook declarations")
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
            # Bound only what the backend materializes before it can allocate it.
            loaded_sizes = [
                archive.getinfo(name).file_size
                for name in _reader_loaded_parts(archive, names, relationship_nodes, limits)
            ]
            if any(size > limits.max_metadata_bytes for size in loaded_sizes) or (
                sum(loaded_sizes) > limits.max_metadata_bytes
            ):
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
            merged_ranges_seen = 0
            worksheet_paths = set()
            for index, sheet in enumerate(sheets):
                visible_sheet = sheet.get("state", "visible") == "visible"
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
                # Per band column: (first and last content row), for spacer joining.
                band_column_extents: dict[int, tuple[int, int]] = {}
                # Per band column: (first visible content row, whether that cell is non-text).
                band_first_cells: dict[int, tuple[int, bool]] = {}
                row_typed_columns: set[int] = set()
                typed_first_rows: set[tuple[int, int]] = set()
                regions: list[TableRegion] = []
                merged_ranges: list[tuple[int, int, int, int]] = []
                merged_columns_by_row: dict[int, list[tuple[int, int]]] = {}
                hidden_rows: set[int] = set()
                hidden_columns: set[int] = set()
                uncached_string_formulas: set[tuple[int, int]] = set()
                # AutoFilter. ``<sheetPr filterMode="1">`` precedes the rows, so on
                # a filtered sheet hidden rows are only candidates while scanning;
                # the filter's ``<autoFilter ref>`` follows the rows, and once it is
                # known the candidates outside every filter range (hidden by hand)
                # rejoin ``hidden_rows``. Excel Tables carry their own AutoFilter.
                sheet_filter_active = False
                sheet_filter_rows: tuple[int, int] | None = None
                filter_candidate_rows: set[int] = set()
                # Past max_tables, blocks sharing columns are merged instead of
                # failing the workbook (see _fold_column_band).
                merging_column_bands = False
                detected_blocks = 0

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

                def fold_block(region: TableRegion) -> None:
                    """Fold one detected block into ``regions``, merging by column band past the table limit."""
                    nonlocal merging_column_bands, regions, detected_blocks
                    detected_blocks += 1
                    # Folding a block costs up to the table width (column sets).
                    # The traversal rectangle only grows, so reject now what the
                    # check after the scan would reject anyway, before a wide
                    # sheet of many small blocks makes that cost add up. Within
                    # the budget, total folding work is bounded by the area.
                    if max_row * max_col > limits.max_sheet_area:
                        raise limit("XLSX worksheet rectangle exceeds the traversal limit")
                    if not merging_column_bands:
                        _fold_preamble(regions, region, explicit_regions, typed_first_rows)
                        if not visible_sheet or len(regions) <= limits.max_tables:
                            return
                        # Too many separate blocks for one table each: refold what
                        # was kept so far by column band, then keep doing so.
                        merging_column_bands = True
                        kept, regions = regions, []
                        for block in kept:
                            _fold_column_band(regions, block, explicit_regions)
                    else:
                        _fold_column_band(regions, region, explicit_regions)
                    # Only layouts that still exceed the limit after merging (more
                    # than max_tables column bands, or blocks split by Excel
                    # Tables) are rejected. The count never decreases, so failing
                    # now matches checking after the scan.
                    if len(regions) > limits.max_tables:
                        raise limit("XLSX worksheet table count exceeds the limit")

                def finish_band():
                    nonlocal band_start, band_end, band_columns, band_first_cells, band_column_extents
                    if band_start is not None and band_end is not None:
                        runs = _join_spacer_columns(
                            list(_column_runs(band_columns)),
                            band_column_extents,
                            band_start,
                            band_end,
                            explicit_regions,
                        )
                        for start_column, end_column, spacers in runs:
                            region = TableRegion(
                                len(regions),
                                band_start,
                                band_end,
                                start_column,
                                end_column,
                                spacer_columns=spacers,
                            )
                            firsts = [
                                band_first_cells[column]
                                for column in range(start_column, end_column + 1)
                                if column in band_first_cells and column not in spacers
                            ]
                            first_row = min((row for row, _ in firsts), default=None)
                            if any(typed for row, typed in firsts if row == first_row):
                                typed_first_rows.add((band_start, start_column))
                            # Fold as the scan goes: a sparse sheet can produce
                            # hundreds of thousands of raw bands, but only the
                            # merged regions are retained.
                            fold_block(region)
                    band_start = band_end = None
                    band_columns = set()
                    band_first_cells = {}
                    band_column_extents = {}

                # Release every completed element so memory stays bounded by one
                # open path, whatever the element (rows, hyperlinks, extLst...).
                # A cell's children are folded into these values as they
                # complete, so a cell with many children retains none of them.
                cell_open = False
                cell_has_value = cell_has_formula = cell_has_inline_text = False
                cell_value_text: str | None = None
                cell_chars = 0
                with archive.open(path) as stream:
                    stack = []
                    for event, element in iterparse(stream, events=("start", "end")):
                        if event == "start":
                            stack.append(element)
                            if element.tag.endswith("}c"):
                                cell_open = True
                                cell_has_value = cell_has_formula = cell_has_inline_text = False
                                cell_value_text = None
                                cell_chars = 0
                            if element.tag.endswith("}row"):
                                current_row = int(element.get("r", "0"))
                                if current_row <= previous_row:
                                    raise invalid("XLSX row coordinates are missing or out of order")
                                if current_row > EXCEL_MAX_ROWS:
                                    raise invalid("XLSX row coordinate is outside the worksheet grid")
                                previous_row, previous_col = current_row, 0
                                advance_explicit_tables(current_row)
                                row_content_columns = set()
                                row_typed_columns = set()
                                if element.get("hidden", "0").lower() in {"1", "true"}:
                                    if sheet_filter_active:
                                        filter_candidate_rows.add(current_row)
                                    else:
                                        hidden_rows.add(current_row)
                            if element.tag.endswith("}c"):
                                match = CELL_REF.fullmatch(element.get("r", ""))
                                if match is None:
                                    raise invalid("XLSX cell is missing a valid coordinate")
                                col = 0
                                for char in match[1]:
                                    col = col * 26 + ord(char) - 64
                                row = int(match[2])
                                if row > EXCEL_MAX_ROWS or col > EXCEL_MAX_COLUMNS:
                                    raise invalid("XLSX cell coordinate is outside the worksheet grid")
                                if row != current_row or col <= previous_col:
                                    raise invalid("XLSX cell coordinates are inconsistent or out of order")
                                previous_col = col
                                current_cell_column = col
                                cells_seen += 1
                                if cells_seen > limits.max_cells:
                                    raise limit("XLSX cell count exceeds the limit")
                            continue
                        tag = element.tag.rsplit("}", 1)[-1]
                        if cell_open and tag != "c":
                            cell_chars += len(element.text or "")
                            if cell_chars > limits.max_cell_chars:
                                raise limit("XLSX cell text exceeds the character limit")
                            direct_child = len(stack) > 1 and stack[-2].tag.endswith("}c")
                            if tag == "v" and direct_child:
                                cell_has_value, cell_value_text = True, element.text
                            elif tag == "f" and direct_child:
                                cell_has_formula = True
                            elif tag == "t" and element.text:
                                cell_has_inline_text = True
                        if tag == "col" and element.get("hidden", "0").lower() in {"1", "true"}:
                            start_column = int(element.get("min", "0"))
                            end_column = int(element.get("max", "0"))
                            if start_column < 1 or end_column < start_column or end_column > 16_384:
                                raise invalid("XLSX hidden column range is invalid")
                            hidden_columns.update(range(start_column, min(end_column, limits.max_columns) + 1))
                        if tag == "c":
                            cell_open = False
                            has_formula = cell_has_formula
                            value_text = cell_value_text
                            has_value = cell_has_value
                            if has_formula and element.get("t") == "str" and not has_value:
                                uncached_string_formulas.add((current_row, current_cell_column))
                            if has_formula and element.get("t") == "str" and has_value and not value_text:
                                # Calculated to an empty string (e.g. =IF(A2="","",...)
                                # filled down a template): an empty cell, not content
                                # and not a formula missing its cached result.
                                has_formula = has_value = False
                                value_text = None
                            if has_formula and current_row not in hidden_rows:
                                visible_formula_columns[current_cell_column] = (
                                    visible_formula_columns.get(current_cell_column, 0) + 1
                                )
                                if not value_text:
                                    missing_formula_columns[current_cell_column] = (
                                        missing_formula_columns.get(current_cell_column, 0) + 1
                                    )
                            if element.get("t") == "s":
                                string_index = int(value_text) if value_text else -1
                                if not 0 <= string_index < shared_count:
                                    raise invalid("XLSX cell references a missing shared string")
                            has_content = has_formula or (has_value and value_text is not None) or cell_has_inline_text
                            if has_content:
                                # Only cells with content bound the reader's work: tables
                                # are read up to their content, so an empty styled cell
                                # far away (leftover formatting) must not reject the file.
                                if current_row > limits.max_rows or current_cell_column > limits.max_columns:
                                    raise limit("XLSX cell coordinate exceeds row/column limits")
                                max_row, max_col = max(max_row, current_row), max(max_col, current_cell_column)
                            if visible_sheet and has_content and not is_in_explicit_table(current_cell_column):
                                row_content_columns.add(current_cell_column)
                                if current_cell_column not in hidden_columns and element.get("t") not in {
                                    "s",
                                    "str",
                                    "inlineStr",
                                }:
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
                                    first, _ = band_column_extents.get(column, (current_row, current_row))
                                    band_column_extents[column] = (first, current_row)
                                if current_row not in hidden_rows:
                                    # Hidden rows are not extracted, so they cannot
                                    # decide what the first extracted row holds.
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
                                or end_row > EXCEL_MAX_ROWS
                                or end_column > EXCEL_MAX_COLUMNS
                            ):
                                raise invalid("XLSX merged cell has an invalid range")
                            if merged_ranges_seen >= limits.max_merged_ranges:
                                raise limit("XLSX merged-cell count exceeds the limit")
                            merged_ranges_seen += 1
                            # A formatting merge may reach past the configured limits
                            # (e.g. a whole-row title merge); only its part inside them
                            # can hold extracted content.
                            if start_row <= limits.max_rows and start_column <= limits.max_columns:
                                end_row = min(end_row, limits.max_rows)
                                end_column = min(end_column, limits.max_columns)
                                merged_ranges.append((start_row, end_row, start_column, end_column))
                                if start_row == end_row and end_column > start_column:
                                    merged_columns_by_row.setdefault(start_row, []).append((start_column, end_column))
                        if tag == "sheetPr" and len(stack) == 2:
                            sheet_filter_active = element.get("filterMode", "0").lower() in {"1", "true"}
                        if tag == "autoFilter" and len(stack) == 2:
                            # Only the worksheet's own filter; custom sheet views
                            # nest theirs and are not what the file shows.
                            sheet_filter_rows = _auto_filter_rows(element)
                        if tag == "tablePart":
                            rel_id = next((value for key, value in element.attrib.items() if key.endswith("}id")), None)
                            if not rel_id:
                                raise invalid("XLSX table part is missing a relationship identifier")
                            table_part_ids.append(rel_id)
                        if len(stack) > 1:
                            stack[-2].remove(element)
                        element.clear()
                        stack.pop()
                finish_band()
                if len(set(table_part_ids)) != len(table_part_ids) or set(table_part_ids) != set(table_relationships):
                    raise invalid("XLSX table parts do not match their relationships")
                filter_ranges = [
                    rows for rows in [sheet_filter_rows, *(region.filter_rows for region in explicit_regions)] if rows
                ]
                filtered_rows = _rows_covered(filter_candidate_rows, filter_ranges)
                # Candidates outside every filter range were hidden by hand. They
                # counted as visible for the scan's formula counts and first-row
                # typing, which errs towards reading more, never less.
                hidden_rows |= filter_candidate_rows - filtered_rows
                formulas = sum(
                    count for column, count in visible_formula_columns.items() if column not in hidden_columns
                )
                missing = sum(
                    count for column, count in missing_formula_columns.items() if column not in hidden_columns
                )
                # ``regions`` was folded during the scan (see fold_block). Column
                # band merging can leave them out of row order; restore it.
                if merging_column_bands:
                    regions.sort(key=lambda region: (region.start_row, region.start_column))
                inferred_regions = _reindexed_regions(regions) if visible_sheet else []
                # Bound the count before combining: _combine_regions never returns
                # fewer regions than it receives (a table absorbs at most one title
                # and is added back), and its work grows with the input.
                if visible_sheet and len(inferred_regions) > limits.max_tables:
                    raise limit("XLSX worksheet table count exceeds the limit")
                regions = _combine_regions(inferred_regions, explicit_regions, merged_ranges) if visible_sheet else []
                header_coverage: dict[int, list[tuple[int, int]]] = {}
                hinted_regions: list[TableRegion] = []
                for region in regions:
                    header_depth = (
                        _merged_header_depth(
                            region, merged_columns_by_row, populated_rows, hidden_rows, header_coverage
                        )
                        if region.header_row_hint is None and region.declared_column_names is None
                        else 0
                    )
                    hinted_regions.append(
                        replace(
                            region,
                            header_row_hint=region.header_row_hint
                            or (region.start_row + header_depth if header_depth else None),
                        )
                    )
                regions = hinted_regions
                # Bound region metadata and the cost of overlapping regions,
                # which can still require separate worksheet scans.
                if visible_sheet and len(regions) > limits.max_tables:
                    raise limit("XLSX worksheet table count exceeds the limit")
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
                        frozenset(uncached_string_formulas),
                        frozenset(filtered_rows),
                        detected_blocks if merging_column_bands else 0,
                    )
                )
            return infos
    except (zipfile.BadZipFile, KeyError, ValueError, ParseError, DefusedXmlException, RuntimeError) as exc:
        raise invalid("File is not a valid XLSX workbook") from exc
    finally:
        if hasattr(source, "seek"):
            source.seek(0)
