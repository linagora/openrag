"""Resolve chart series references that carry no cached values.

A writer that stores no cache (OpenPyXL) leaves only the formula, for example
``'Sales'!$B$2:$B$13``. The values are then read from the worksheet cells.

Uncached ranges are grouped into bounded read windows per referenced sheet;
distant ranges must not create a huge mostly empty rectangle. Only single-area
references inside the workbook are resolved; a union (``(A1:A3,C1:C3)``), a
defined name, or a reference to another workbook (``[1]Sheet1!A1``) is left
unresolved and reported as such.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

_REFERENCE = re.compile(
    r"""^(?:'(?P<quoted>(?:[^']|'')+)'|(?P<plain>[^'!\[\]()*?:/\\,]+))!"""
    r"""\$?(?P<c1>[A-Za-z]{1,3})\$?(?P<r1>[1-9][0-9]{0,6})"""
    r"""(?::\$?(?P<c2>[A-Za-z]{1,3})\$?(?P<r2>[1-9][0-9]{0,6}))?$"""
)


@dataclass(frozen=True)
class CellRange:
    sheet: str
    min_row: int
    min_column: int
    max_row: int
    max_column: int

    @property
    def area(self) -> int:
        return (self.max_row - self.min_row + 1) * (self.max_column - self.min_column + 1)

    @property
    def reference(self) -> str:
        """The range in A1 form, sheet quoted when needed: ``'My Sheet'!$B$2:$B$4``."""
        plain = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", self.sheet) is not None
        sheet = self.sheet if plain else "'" + self.sheet.replace("'", "''") + "'"
        start = f"${_column_name(self.min_column)}${self.min_row}"
        end = f"${_column_name(self.max_column)}${self.max_row}"
        return f"{sheet}!{start}" if start == end else f"{sheet}!{start}:{end}"


def _column_name(number: int) -> str:
    name = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _column_number(label: str) -> int:
    number = 0
    for character in label.upper():
        number = number * 26 + ord(character) - 64
    return number


def parse_reference(formula: str | None) -> CellRange | None:
    """Parse a single-area ``Sheet!A1:B2`` reference, or return ``None``."""
    if not formula:
        return None
    match = _REFERENCE.match(formula.strip())
    if match is None:
        return None
    sheet = match["quoted"].replace("''", "'") if match["quoted"] is not None else match["plain"].strip()
    first = (int(match["r1"]), _column_number(match["c1"]))
    last = (int(match["r2"]), _column_number(match["c2"])) if match["c2"] else first
    return CellRange(
        sheet,
        min(first[0], last[0]),
        min(first[1], last[1]),
        max(first[0], last[0]),
        max(first[1], last[1]),
    )


#: Yields rows of cell values for (sheet, min_row, max_row, min_column, max_column),
#: or of cell number formats when called with ``formats=True``; raises
#: KeyError for an unknown sheet.
RowReader = Callable[..., Iterator[tuple[Any, ...]]]

_MAX_READ_WINDOW_CELLS = 100_000


def _read_windows(ranges: set[CellRange]) -> Iterator[set[CellRange]]:
    """Group nearby bounded ranges without expanding their rectangle excessively."""
    group: set[CellRange] = set()
    bounds = None
    for item in sorted(ranges, key=lambda r: (r.min_row, r.min_column, r.max_row, r.max_column)):
        candidate = (
            (item.min_row, item.min_column, item.max_row, item.max_column)
            if bounds is None
            else (
                min(bounds[0], item.min_row),
                min(bounds[1], item.min_column),
                max(bounds[2], item.max_row),
                max(bounds[3], item.max_column),
            )
        )
        if group and (candidate[2] - candidate[0] + 1) * (candidate[3] - candidate[1] + 1) > _MAX_READ_WINDOW_CELLS:
            yield group
            group = set()
            candidate = (item.min_row, item.min_column, item.max_row, item.max_column)
        group.add(item)
        bounds = candidate
    if group:
        yield group


def series_values(
    cell_range: CellRange,
    grid: list[list[str]],
    *,
    hidden_rows: frozenset[int] = frozenset(),
    hidden_columns: frozenset[int] = frozenset(),
) -> tuple[str, ...]:
    """Flatten a range the way Excel reads a series: along its longer side.

    A two-dimensional category range (several label columns) becomes one
    combined label per point, outer label first: ``2025 / Q1``. Rows and
    columns passed as hidden are dropped first, as Excel does for a chart that
    plots visible cells only (its default).
    """
    # The grid may hold only the first cells of the range (see resolve_ranges).
    width = len(grid[0]) if grid else 0
    rows = [index for index in range(len(grid)) if cell_range.min_row + index not in hidden_rows]
    columns = [index for index in range(width) if cell_range.min_column + index not in hidden_columns]
    if not rows or not columns:
        return ()
    # The orientation is the range's, not what is left once cells are hidden.
    vertical = cell_range.max_row - cell_range.min_row >= cell_range.max_column - cell_range.min_column
    points = (
        [[grid[row][column] for column in columns] for row in rows]
        if vertical
        else [[grid[row][column] for row in rows] for column in columns]
    )
    return tuple(" / ".join(label for label in labels if label) for labels in _carried(points))


def _carried(points: list[list[str]]) -> list[list[str]]:
    """Carry outer labels of a hierarchy over the points of their group.

    Excel writes a group label once, on its first row, and leaves it blank
    below: ``Best | First | A`` then ``| | B``. A blank level takes the label
    above it while every level before it is blank too, so the second point is
    ``Best / First / B``; once a level has its own label, the levels after it
    are this point's own. The last level, the point itself, is never carried.
    """
    if not points or len(points[0]) < 2:
        return points
    carried = []
    previous: list[str] = []
    for labels in points:
        first = next((index for index, label in enumerate(labels) if label), len(labels))
        outer = min(first, len(labels) - 1)
        row = (previous[:outer] if len(previous) >= outer else [""] * outer) + labels[outer:]
        carried.append(row)
        previous = row
    return carried


def resolve_ranges(
    ranges: Iterable[CellRange],
    read_rows: RowReader,
    cell_text: Callable[[Any], str],
    *,
    max_cells: int,
) -> dict[CellRange, list[list[str]]]:
    """Read the cell texts of ``ranges``, row by row; an unreadable range is absent.

    Only the first ``max_cells`` cells of a larger range are read, along its
    longer side, so a long series is shown in part rather than not at all; the
    grid is then shorter than the range. Nearby ranges share bounded read
    windows. :func:`series_values` turns a grid
    into a series.
    """
    parts = {cell_range: _clamped(cell_range, min(max_cells, _MAX_READ_WINDOW_CELLS)) for cell_range in set(ranges)}
    by_sheet: dict[str, dict[CellRange, CellRange]] = {}
    for requested, part in parts.items():
        by_sheet.setdefault(requested.sheet, {})[requested] = part
    resolved: dict[CellRange, list[list[str]]] = {}
    for sheet, requested_parts in by_sheet.items():
        grids = {}
        for window in _read_windows(set(requested_parts.values())):
            grids.update(_read_sheet(sheet, window, read_rows, cell_text))
        resolved.update({requested: grids[part] for requested, part in requested_parts.items() if part in grids})
    return resolved


def _clamped(cell_range: CellRange, max_cells: int) -> CellRange:
    """Keep the first ``max_cells`` cells of a range, along its longer side."""
    if cell_range.area <= max_cells:
        return cell_range
    rows = cell_range.max_row - cell_range.min_row + 1
    columns = cell_range.max_column - cell_range.min_column + 1
    if rows >= columns:
        if columns > max_cells:
            return CellRange(
                cell_range.sheet,
                cell_range.min_row,
                cell_range.min_column,
                cell_range.min_row,
                cell_range.min_column + max_cells - 1,
            )
        last_row = cell_range.min_row + max(1, max_cells // columns) - 1
        return CellRange(cell_range.sheet, cell_range.min_row, cell_range.min_column, last_row, cell_range.max_column)
    if rows > max_cells:
        return CellRange(
            cell_range.sheet,
            cell_range.min_row,
            cell_range.min_column,
            cell_range.min_row + max_cells - 1,
            cell_range.min_column,
        )
    last_column = cell_range.min_column + max(1, max_cells // rows) - 1
    return CellRange(cell_range.sheet, cell_range.min_row, cell_range.min_column, cell_range.max_row, last_column)


def _read_sheet(
    sheet: str, wanted: set[CellRange], read_rows: RowReader, cell_text: Callable[[Any], str]
) -> dict[CellRange, list[list[str]]]:
    """Read the ranges of one sheet in a single pass; an unknown sheet reads nothing."""
    min_row = min(cell_range.min_row for cell_range in wanted)
    max_row = max(cell_range.max_row for cell_range in wanted)
    min_column = min(cell_range.min_column for cell_range in wanted)
    max_column = max(cell_range.max_column for cell_range in wanted)
    grids: dict[CellRange, list[list[str]]] = {cell_range: [] for cell_range in wanted}
    try:
        for row_number, row in enumerate(read_rows(sheet, min_row, max_row, min_column, max_column), min_row):
            for cell_range, grid in grids.items():
                if cell_range.min_row <= row_number <= cell_range.max_row:
                    start = cell_range.min_column - min_column
                    width = cell_range.max_column - cell_range.min_column + 1
                    cells = list(row[start : start + width])
                    cells += [None] * (width - len(cells))
                    grid.append([cell_text(value).strip() for value in cells])
    except KeyError:
        return {}
    for cell_range, grid in grids.items():
        # A range below the last stored row reads as empty cells.
        width = cell_range.max_column - cell_range.min_column + 1
        grid += [[""] * width for _ in range(cell_range.max_row - cell_range.min_row + 1 - len(grid))]
    return grids


def first_cell_formats(ranges: Iterable[CellRange], read_rows: RowReader) -> dict[CellRange, str]:
    """Return the number format of each range's first cell in bounded windows.

    A chart's category cache may say ``General`` while the cells are dates:
    Excel then labels the axis from the cells, and so must the index.
    """
    by_sheet: dict[str, set[CellRange]] = {}
    for cell_range in set(ranges):
        by_sheet.setdefault(cell_range.sheet, set()).add(cell_range)
    found: dict[CellRange, str] = {}
    for sheet, wanted in by_sheet.items():
        by_cell: dict[tuple[int, int], list[CellRange]] = {}
        for cell_range in wanted:
            by_cell.setdefault((cell_range.min_row, cell_range.min_column), []).append(cell_range)
        points = {CellRange(sheet, row, col, row, col) for row, col in by_cell}
        for window in _read_windows(points):
            min_row = min(point.min_row for point in window)
            max_row = max(point.max_row for point in window)
            min_column = min(point.min_column for point in window)
            max_column = max(point.max_column for point in window)
            try:
                rows = read_rows(sheet, min_row, max_row, min_column, max_column, formats=True)
                for row_number, row in enumerate(rows, min_row):
                    for offset, number_format in enumerate(row):
                        for cell_range in by_cell.get((row_number, min_column + offset), ()):
                            if number_format:
                                found[cell_range] = number_format
            except KeyError:
                continue
    return found
