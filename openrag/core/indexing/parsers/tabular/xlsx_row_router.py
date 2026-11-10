"""Serve every table of a worksheet from one forward-only scan.

Read-only OpenPyXL parses each worksheet row in full whatever column window is
requested, so reading N side-by-side tables with N windowed scans parses the
sheet N times (twice that with the formula reader). The router reads the
bounding box of the sheet's tables once and hands each row slice to every
table that covers it.

Tables are still *consumed* one after another, in their declared order: the
parser's header detection, batching and formula grouping keep their state per
table. Rows that belong to a table whose turn has not come yet are therefore
kept until it does. They are first converted to plain values by that table's
own row pipeline, fed one row at a time so its state (hidden rows, expanded
row-header merges) carries across rows exactly as in a dedicated scan, then
written to one anonymous temporary file per sheet. Memory stays bounded by a
small buffer per table whatever the layout.

The temporary file is bounded by ``spool_limit`` bytes. When a sheet would need
more, spooling stops and every table still waiting gets its own windowed scan
when its turn comes, which is the previous behaviour: the worst case costs what
the parser cost before, and never more disk than the limit.
"""

from __future__ import annotations

import io
import pickle
import tempfile
from array import array
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack
from itertools import islice
from typing import Any

from core.utils.logging import get_logger

logger = get_logger()

#: Bytes a waiting table keeps in memory before its rows are written out as one
#: block. Bounded per table, so a sheet with many tables holds at most
#: ``tables * _SPOOL_BLOCK_BYTES`` (plus one row) in these buffers.
_SPOOL_BLOCK_BYTES = 64 * 1024
_PICKLE_PROTOCOL = pickle.HIGHEST_PROTOCOL

#: ``open_scan(min_row, max_row, min_column, max_column)`` returns a context
#: manager yielding ``(value_rows, formula_rows)``: two row iterators over the
#: same rectangle, ``formula_rows`` being ``None`` when the sheet has no
#: formulas. Each row is a sequence exactly as wide as the rectangle.
OpenScan = Callable[[int, int, int, int], AbstractContextManager[tuple[Iterator, Iterator | None]]]
#: ``make_pipeline(table, value_rows, formula_rows)`` maps a table's raw row
#: slices to its output rows, exactly one output row per input row.
MakePipeline = Callable[[Any, Iterator, Iterator | None], Iterator]

_NO_ROW = object()


class _Feed:
    """An iterator pushed one row at a time, to drive a pipeline row by row."""

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: deque = deque()

    def push(self, row) -> None:
        self._rows.append(row)

    def __iter__(self) -> _Feed:
        return self

    def __next__(self):
        if not self._rows:
            # Row pipelines map one input row to one output row, so a pull can
            # never outrun a push. Raising StopIteration here would silently end
            # the pipeline instead of surfacing the broken invariant.
            raise RuntimeError("XLSX row pipeline read ahead of the worksheet scan")
        return self._rows.popleft()


class _Route:
    """A table's place in the shared scan, its row pipeline and its spooled rows."""

    __slots__ = ("table", "start", "end", "values", "formulas", "pipeline", "blocks", "buffer", "done", "rescan")

    def __init__(self, table, first_column: int, with_formulas: bool, make_pipeline: MakePipeline) -> None:
        self.table = table
        self.start = table.start_column - first_column
        self.end = table.end_column - first_column + 1
        self.values: _Feed | None = _Feed()
        self.formulas: _Feed | None = _Feed() if with_formulas else None
        self.pipeline: Iterator | None = make_pipeline(table, self.values, self.formulas)
        # Flattened (offset, length) pairs of the spooled blocks, in row order.
        self.blocks = array("Q")
        self.buffer = bytearray()
        self.done = False
        self.rescan = False

    def release(self) -> None:
        self.pipeline = None
        self.values = self.formulas = None
        self.blocks = array("Q")
        self.buffer = bytearray()


class _Spool:
    """One anonymous temporary file shared by every waiting table of a sheet."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.reserved = 0
        self._file = None
        self._end = 0

    def reserve(self, size: int) -> bool:
        """Account ``size`` bytes against the limit; ``False`` when they do not fit."""
        if self.reserved + size > self.limit:
            return False
        self.reserved += size
        return True

    def write(self, data: bytes | bytearray) -> tuple[int, int]:
        if self._file is None:
            # Unlinked on creation on POSIX: nothing is left behind even if the
            # worker dies mid-parse. Honours TMPDIR like every tempfile user.
            self._file = tempfile.TemporaryFile(prefix="openrag-xlsx-")
        self._file.seek(self._end)
        self._file.write(data)
        offset = self._end
        self._end += len(data)
        return offset, len(data)

    def read(self, offset: int, length: int) -> bytes:
        self._file.seek(offset)
        data = self._file.read(length)
        if len(data) != length:
            raise OSError("XLSX row spool was truncated")
        return data

    def close(self) -> None:
        if self._file is not None:
            spool, self._file = self._file, None
            try:
                spool.close()
            except OSError:
                # The rows were already served; a failed close must not fail
                # the parse. The file is unlinked, so nothing is left behind.
                logger.warning("Could not close the XLSX row spool", exc_info=True)


def _unpickle_rows(data: bytes) -> Iterator:
    stream = io.BytesIO(data)
    while stream.tell() < len(data):
        yield pickle.load(stream)


class _SharedScan:
    """The single forward-only scan and the routing of each row it reads."""

    def __init__(
        self,
        routes: list[_Route],
        value_rows: Iterator,
        formula_rows: Iterator | None,
        first_row: int,
        spool: _Spool,
    ) -> None:
        self.routes = sorted(routes, key=lambda route: route.table.start_row)
        self.value_rows = value_rows
        self.formula_rows = formula_rows
        self.next_row = first_row
        self.exhausted = False
        self.spool = spool
        self.spooling = True
        self.current: _Route | None = None
        self._pending = 0
        self._active: list[_Route] = []

    def _covering(self, row_number: int) -> list[_Route]:
        # Tables enter when the scan reaches their first row and leave after
        # their last, so each row costs only the tables that actually cover it.
        while self._pending < len(self.routes) and self.routes[self._pending].table.start_row <= row_number:
            self._active.append(self.routes[self._pending])
            self._pending += 1
        self._active = [
            route for route in self._active if route.table.end_row >= row_number and not route.done and not route.rescan
        ]
        return self._active

    def advance(self) -> Any:
        """Read one row and route it; return the current table's row, if it covers it."""
        try:
            values = next(self.value_rows)
        except StopIteration:
            self.exhausted = True
            if self.formula_rows is not None and next(self.formula_rows, _NO_ROW) is not _NO_ROW:
                # A dedicated scan rejects this through its strict zip; the
                # shared scan must not accept the shorter reader silently.
                raise ValueError("XLSX value and formula readers disagree on the row count") from None
            return _NO_ROW
        formulas = None
        if self.formula_rows is not None:
            try:
                formulas = next(self.formula_rows)
            except StopIteration as exc:
                raise ValueError("XLSX value and formula readers disagree on the row count") from exc
        row_number = self.next_row
        self.next_row += 1
        current_row = _NO_ROW
        for route in self._covering(row_number):
            if route.rescan:
                # Spooling stopped earlier in this same row: this table was
                # handed back to its own scan and no longer has a pipeline.
                continue
            route.values.push(values[route.start : route.end])
            if route.formulas is not None:
                route.formulas.push(formulas[route.start : route.end])
            row = next(route.pipeline)
            if route is self.current:
                current_row = row
            else:
                self._spool_row(route, row)
        return current_row

    def _spool_row(self, route: _Route, row) -> None:
        if not self.spooling:
            return
        try:
            data = pickle.dumps(row, protocol=_PICKLE_PROTOCOL)
        except (pickle.PicklingError, TypeError, AttributeError):
            self._stop_spooling()
            return
        if not self.spool.reserve(len(data)):
            self._stop_spooling()
            return
        route.buffer += data
        if len(route.buffer) >= _SPOOL_BLOCK_BYTES:
            try:
                block = self.spool.write(route.buffer)
            except OSError:
                # No usable temporary storage (full disk, read-only TMPDIR):
                # the workbook is still valid, so read waiting tables the way
                # they were read before rather than failing ingestion.
                logger.warning("XLSX row spool unavailable; waiting tables use their own scans", exc_info=True)
                self._stop_spooling()
                return
            route.blocks.extend(block)
            route.buffer = bytearray()

    def _stop_spooling(self) -> None:
        """Hand every waiting table back to its own scan; disk stays within the limit."""
        self.spooling = False
        for route in self.routes:
            if route is not self.current and not route.done:
                route.rescan = True
                route.release()

    def replay(self, route: _Route) -> Iterator:
        blocks = route.blocks
        for index in range(0, len(blocks), 2):
            yield from _unpickle_rows(self.spool.read(blocks[index], blocks[index + 1]))
        buffered = bytes(route.buffer)
        route.blocks = array("Q")
        route.buffer = bytearray()
        yield from _unpickle_rows(buffered)


def route_sheet_tables(
    tables: list,
    open_scan: OpenScan,
    make_pipeline: MakePipeline,
    *,
    with_formulas: bool,
    spool_limit: int,
    max_scan_area: int | None = None,
    has_columns: Callable[[Any], bool] = lambda table: True,
) -> Iterator[tuple[Any, Iterator]]:
    """Yield ``(table, rows)`` for each table, in order, from one shared scan.

    ``rows`` yields exactly what ``make_pipeline`` would yield over a dedicated
    scan of that table's rectangle. It must be consumed (or abandoned) before
    the next pair is requested, which is how the parser reads tables. A table
    for which ``has_columns`` is false gets an empty iterator and is never
    scanned.

    A scanned row is as wide as the tables' bounding box, empty cells included.
    When that box is larger than ``max_scan_area`` (a tall declared table beside
    a wide one), sharing would cost more than it saves, so every table is read
    with its own scan instead, as before.
    """
    routed = [table for table in tables if has_columns(table)]
    if not routed:
        for table in tables:
            yield table, iter(())
        return

    first_row = min(table.start_row for table in routed)
    last_row = max(table.end_row for table in routed)
    first_column = min(table.start_column for table in routed)
    last_column = max(table.end_column for table in routed)

    def own_scan(table) -> Iterator:
        bounds = (table.start_row, table.end_row, table.start_column, table.end_column)
        with open_scan(*bounds) as (own_values, own_formulas):
            yield from make_pipeline(table, iter(own_values), iter(own_formulas) if with_formulas else None)

    area = (last_row - first_row + 1) * (last_column - first_column + 1)
    if max_scan_area is not None and area > max_scan_area:
        shared_tables = {id(table) for table in routed}
        for table in tables:
            rows = own_scan(table) if id(table) in shared_tables else iter(())
            try:
                yield table, rows
            finally:
                close = getattr(rows, "close", None)
                if close is not None:
                    close()
        return

    spool = _Spool(spool_limit)
    with ExitStack() as stack:
        stack.callback(spool.close)
        value_rows, formula_rows = stack.enter_context(open_scan(first_row, last_row, first_column, last_column))
        if with_formulas and formula_rows is None:
            raise ValueError("XLSX formula reader is unavailable")
        routes = {id(table): _Route(table, first_column, with_formulas, make_pipeline) for table in routed}
        scan = _SharedScan(
            list(routes.values()),
            iter(value_rows),
            iter(formula_rows) if with_formulas else None,
            first_row,
            spool,
        )

        def table_rows(route: _Route) -> Iterator:
            scan.current = route
            try:
                if route.rescan:
                    yield from own_scan(route.table)
                    return
                emitted = 0
                try:
                    for row in scan.replay(route):
                        yield row
                        emitted += 1
                except OSError:
                    # The spool was written but cannot be read back. The table's
                    # rows are deterministic, so a dedicated scan resumes after
                    # the rows already emitted; waiting tables do the same.
                    logger.warning("XLSX row spool unreadable; falling back to dedicated scans", exc_info=True)
                    scan._stop_spooling()
                    route.rescan = True
                    route.release()
                    yield from islice(own_scan(route.table), emitted, None)
                    return
                while not scan.exhausted and scan.next_row <= route.table.end_row:
                    row = scan.advance()
                    if row is not _NO_ROW:
                        yield row
            finally:
                route.done = True
                route.release()
                scan.current = None

        for table in tables:
            route = routes.get(id(table))
            if route is None:
                yield table, iter(())
                continue
            rows = table_rows(route)
            try:
                yield table, rows
            finally:
                # The consumer moved on: whatever it left unread is dropped,
                # and later rows of this table are no longer routed to it.
                # Closing a generator that never started skips its own
                # cleanup, so retire the route here as well.
                rows.close()
                route.done = True
                route.release()
