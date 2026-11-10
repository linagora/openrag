"""The shared worksheet scan must yield exactly what a dedicated scan per table yields.

These tests drive ``route_sheet_tables`` with an in-memory grid and a stateful
pipeline, then compare every table's rows with the reference: the same
pipeline over that table's own rectangle. Layouts, consumption order, partial
consumption and the spool budget are all varied, because each changes which
rows are routed live, spooled, or handed back to a dedicated scan.
"""

from __future__ import annotations

import random
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
from core.indexing.parsers.tabular import xlsx_row_router as router
from core.indexing.parsers.tabular.xlsx_row_router import route_sheet_tables


@dataclass(eq=False)
class _Table:
    name: str
    start_row: int
    end_row: int
    start_column: int
    end_column: int


@dataclass
class _Grid:
    """A worksheet: ``values[r][c]`` is the cell at row r+1, column c+1."""

    values: list[list]
    formulas: list[list] | None = None
    scans: list[tuple[int, int, int, int]] = field(default_factory=list)

    def open_scan(self, min_row, max_row, min_column, max_column):
        @contextmanager
        def scan():
            self.scans.append((min_row, max_row, min_column, max_column))

            def rows(grid):
                for row in grid[min_row - 1 : max_row]:
                    yield tuple(row[min_column - 1 : max_column])

            yield rows(self.values), rows(self.formulas) if self.formulas is not None else None

        return scan()


def _pipeline(table, values, formulas):
    """Stateful, one output per input: a running total carried across rows
    catches any row replayed out of order, dropped or fed to the wrong table."""
    total = 0
    for index, row in enumerate(values):
        formula_row = next(formulas) if formulas is not None else None
        total += sum(value for value in row if isinstance(value, int))
        yield (table.name, index, row, formula_row, total)


def _reference(grid: _Grid, table: _Table) -> list:
    with grid.open_scan(table.start_row, table.end_row, table.start_column, table.end_column) as (values, formulas):
        return list(_pipeline(table, values, formulas))


def _consume(
    grid,
    tables,
    *,
    spool_limit,
    spool_memory_limit=router.DEFAULT_SPOOL_MEMORY_BYTES,
    take=None,
    with_formulas=False,
    has_columns=None,
    max_scan_area=None,
):
    grid.scans.clear()
    taken = {}
    for table, rows in route_sheet_tables(
        tables,
        grid.open_scan,
        _pipeline,
        with_formulas=with_formulas,
        spool_limit=spool_limit,
        spool_memory_limit=spool_memory_limit,
        max_scan_area=max_scan_area,
        **({"has_columns": has_columns} if has_columns else {}),
    ):
        limit = None if take is None else take.get(table.name)
        out = []
        for row in rows:
            if limit is not None and len(out) >= limit:
                break
            out.append(row)
        taken[table.name] = out
    return taken


def _side_by_side(tables=3, rows=200, width=2, gap=2, formulas=False) -> tuple[_Grid, list[_Table]]:
    columns = tables * (width + gap)
    values = [[(r * 1000 + c) for c in range(columns)] for r in range(rows)]
    layout = [_Table(f"t{t}", 1, rows, 1 + t * (width + gap), t * (width + gap) + width) for t in range(tables)]
    grid = _Grid(values, [[f"=F{r}_{c}" for c in range(columns)] for r in range(rows)] if formulas else None)
    return grid, layout


def test_side_by_side_tables_share_one_scan_and_match_dedicated_scans(monkeypatch):
    # Small blocks and no memory tier force waiting rows through the file and
    # back, so a spool losing them on disk could not pass on in-memory buffers.
    reads = []
    original_read = router._Spool.read

    def read(self, handle):
        data = original_read(self, handle)
        reads.append(len(data))
        return data

    monkeypatch.setattr(router._Spool, "read", read)
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 256)
    grid, tables = _side_by_side(tables=8)
    taken = _consume(grid, tables, spool_limit=1 << 30, spool_memory_limit=0)

    assert len(grid.scans) == 1
    assert reads
    for table in tables:
        assert taken[table.name] == _reference(grid, table)


def test_formula_rows_stay_paired_with_their_values():
    grid, tables = _side_by_side(tables=4, formulas=True)
    taken = _consume(grid, tables, spool_limit=1 << 30, with_formulas=True)

    assert len(grid.scans) == 1
    for table in tables:
        assert taken[table.name] == _reference(grid, table)


def test_tables_declared_out_of_row_order_are_served_in_declared_order():
    grid, _ = _side_by_side(tables=1, rows=300, width=3)
    bottom = _Table("bottom", 201, 300, 1, 3)
    top = _Table("top", 1, 100, 1, 3)
    middle = _Table("middle", 101, 200, 1, 3)
    taken = _consume(grid, [bottom, top, middle], spool_limit=1 << 30)

    assert list(taken) == ["bottom", "top", "middle"]
    for table in (bottom, top, middle):
        assert taken[table.name] == _reference(grid, table)


def test_exhausted_spool_budget_falls_back_to_dedicated_scans_without_changing_rows():
    grid, tables = _side_by_side(tables=5)
    taken = _consume(grid, tables, spool_limit=1)

    # The first table rides the shared scan; every waiting table rescans.
    assert len(grid.scans) == 1 + 4
    for table in tables:
        assert taken[table.name] == _reference(grid, table)


def test_budget_exhausted_while_routing_a_row_skips_tables_released_in_that_row():
    """Regression: stopping the spool releases every waiting table, including ones
    the same row has not been routed to yet. They must be skipped, not fed."""
    grid, tables = _side_by_side(tables=6, rows=50)
    for limit in range(1, 4000, 97):
        taken = _consume(grid, tables, spool_limit=limit)
        for table in tables:
            assert taken[table.name] == _reference(grid, table), limit


@pytest.mark.parametrize("memory_limit", [0, 3_000, 1 << 30])
def test_spool_never_holds_more_than_its_limits(monkeypatch, memory_limit):
    peak_total = peak_memory = 0
    original_write = router._Spool.write

    def write(self, data):
        nonlocal peak_total, peak_memory
        result = original_write(self, data)
        peak_total = max(peak_total, self.on_disk + self.in_memory)
        peak_memory = max(peak_memory, self.in_memory)
        return result

    monkeypatch.setattr(router._Spool, "write", write)
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 256)
    grid, tables = _side_by_side(tables=6, rows=400)
    for limit in (512, 5_000, 50_000):
        peak_total = peak_memory = 0
        taken = _consume(grid, tables, spool_limit=limit, spool_memory_limit=memory_limit)
        assert peak_total <= limit
        assert peak_memory <= memory_limit
        for table in tables:
            assert taken[table.name] == _reference(grid, table)


def test_a_partially_read_table_does_not_disturb_the_next_ones():
    grid, tables = _side_by_side(tables=4, rows=120)
    taken = _consume(grid, tables, spool_limit=1 << 30, take={"t0": 3, "t1": 0, "t2": 60})

    assert taken["t0"] == _reference(grid, tables[0])[:3]
    assert taken["t1"] == []
    assert taken["t2"] == _reference(grid, tables[2])[:60]
    assert taken["t3"] == _reference(grid, tables[3])


def test_tables_without_visible_columns_are_never_scanned():
    grid, tables = _side_by_side(tables=3)
    taken = _consume(grid, tables, spool_limit=1 << 30, has_columns=lambda table: table.name != "t1")

    assert taken["t1"] == []
    assert grid.scans == [(1, 200, tables[0].start_column, tables[2].end_column)]
    assert taken["t2"] == _reference(grid, tables[2])


def test_value_and_formula_readers_must_agree_on_row_count():
    grid, tables = _side_by_side(tables=2, rows=20, formulas=True)
    grid.formulas = grid.formulas[:10]
    with pytest.raises(ValueError, match="disagree on the row count"):
        _consume(grid, tables, spool_limit=1 << 30, with_formulas=True)


def test_a_pipeline_reading_ahead_is_a_loud_error_not_a_silent_truncation():
    grid, tables = _side_by_side(tables=2, rows=10)

    def greedy(table, values, formulas):
        for first in values:
            yield first, next(values)

    with pytest.raises(RuntimeError, match="read ahead"):
        for _, rows in route_sheet_tables(tables, grid.open_scan, greedy, with_formulas=False, spool_limit=1 << 30):
            list(rows)


def test_the_spool_file_is_closed_when_the_consumer_stops_early(monkeypatch):
    closed = []
    original_close = router._Spool.close

    def close(self):
        closed.append(self._file is not None)
        original_close(self)

    monkeypatch.setattr(router._Spool, "close", close)
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 64)
    grid, tables = _side_by_side(tables=3)
    pairs = route_sheet_tables(
        tables, grid.open_scan, _pipeline, with_formulas=False, spool_limit=1 << 30, spool_memory_limit=0
    )
    _, rows = next(pairs)
    list(rows)  # spools the other two tables to the file
    pairs.close()

    assert closed == [True]


@pytest.mark.parametrize("seed", range(40))
def test_random_layouts_consumption_and_budgets_match_dedicated_scans(seed, monkeypatch):
    rng = random.Random(seed)
    rows, columns = rng.randint(5, 120), rng.randint(4, 30)
    values = [[rng.choice([None, rng.randint(0, 99), f"s{r}{c}"]) for c in range(columns)] for r in range(rows)]
    with_formulas = rng.random() < 0.5
    grid = _Grid(values, [[f"={r}:{c}" for c in range(columns)] for r in range(rows)] if with_formulas else None)

    # Non-overlapping rectangles: side by side, stacked, staggered, in random order.
    tables, occupied = [], set()
    for index in range(rng.randint(1, 8)):
        for _ in range(20):
            top, left = rng.randint(1, rows), rng.randint(1, columns)
            bottom, right = rng.randint(top, rows), rng.randint(left, min(columns, left + 4))
            cells = {(r, c) for r in range(top, bottom + 1) for c in range(left, right + 1)}
            if not cells & occupied:
                occupied |= cells
                tables.append(_Table(f"t{index}", top, bottom, left, right))
                break
    rng.shuffle(tables)
    take = {
        table.name: rng.choice([None, None, 0, rng.randint(0, table.end_row - table.start_row + 1)]) for table in tables
    }
    spool_limit = rng.choice([1, 50, 2_000, 1 << 30])
    memory_limit = rng.choice([0, 300, 1 << 30])
    if rng.random() < 0.5:
        monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", rng.choice([16, 128]))

    taken = _consume(
        grid, tables, spool_limit=spool_limit, spool_memory_limit=memory_limit, take=take, with_formulas=with_formulas
    )

    for table in tables:
        expected = _reference(grid, table)
        limit = take[table.name]
        assert taken[table.name] == (expected if limit is None else expected[:limit])


def test_a_formula_reader_longer_than_the_value_reader_is_rejected():
    """Both orders of mismatch fail, as a dedicated scan's strict zip does."""
    grid, tables = _side_by_side(tables=2, rows=20, formulas=True)
    grid.values = grid.values[:10]
    with pytest.raises(ValueError, match="disagree on the row count"):
        _consume(grid, tables, spool_limit=1 << 30, with_formulas=True)


def test_unusable_temporary_storage_falls_back_to_dedicated_scans(monkeypatch):
    """Past the memory tier, a full disk or a read-only TMPDIR must not fail a valid workbook."""

    def no_storage(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(router.tempfile, "TemporaryFile", no_storage)
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 64)
    grid, tables = _side_by_side(tables=4)
    taken = _consume(grid, tables, spool_limit=1 << 30, spool_memory_limit=0)

    assert len(grid.scans) == 1 + 3
    for table in tables:
        assert taken[table.name] == _reference(grid, table)


def test_a_failing_spool_close_does_not_fail_the_parse(monkeypatch):
    class _Unclosable:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def close(self):
            self._real.close()
            raise OSError("close failed")

    real_temporary_file = router.tempfile.TemporaryFile
    monkeypatch.setattr(router.tempfile, "TemporaryFile", lambda **kwargs: _Unclosable(real_temporary_file(**kwargs)))
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 64)
    grid, tables = _side_by_side(tables=3)
    taken = _consume(grid, tables, spool_limit=1 << 30, spool_memory_limit=0)

    for table in tables:
        assert taken[table.name] == _reference(grid, table)


def test_a_table_skipped_without_reading_is_no_longer_spooled(monkeypatch):
    """Closing a never-started generator skips its own cleanup; the router must
    still retire the route, or it keeps spending spool on a table nobody reads."""
    spooled = []
    original_spool_row = router._SharedScan._spool_row

    def recording_spool_row(self, route, row):
        spooled.append(route.table.name)
        original_spool_row(self, route, row)

    monkeypatch.setattr(router._SharedScan, "_spool_row", recording_spool_row)
    grid, _ = _side_by_side(tables=1, rows=200, width=12)
    first = _Table("first", 1, 50, 1, 2)
    skipped = _Table("skipped", 1, 200, 5, 6)
    last = _Table("last", 1, 200, 9, 10)
    taken = {}
    for table, rows in route_sheet_tables(
        [first, skipped, last], grid.open_scan, _pipeline, with_formulas=False, spool_limit=1 << 30
    ):
        if table is not skipped:  # the skipped table's iterator is never touched
            taken[table.name] = list(rows)

    # Only the 50 rows read while serving "first", none while serving "last".
    assert spooled.count("skipped") == 50
    assert taken["last"] == _reference(grid, last)


def test_a_bounding_box_past_the_scan_budget_falls_back_to_dedicated_scans():
    """A scanned row is as wide as the tables' bounding box: a tall declared table
    beside a wide one would make the shared scan build mostly empty cells."""
    grid = _Grid([[(r * 100 + c) for c in range(40)] for r in range(300)])
    tall = _Table("tall", 1, 300, 1, 1)
    wide = _Table("wide", 1, 2, 3, 40)
    budget = 300 + 2 * 38  # what the two dedicated scans cost, far below 300 x 40

    taken = _consume(grid, [tall, wide], spool_limit=1 << 30, max_scan_area=budget)

    assert grid.scans == [(1, 300, 1, 1), (1, 2, 3, 40)]
    assert taken["tall"] == _reference(grid, tall)
    assert taken["wide"] == _reference(grid, wide)


def test_a_bounding_box_within_the_scan_budget_is_shared():
    grid, tables = _side_by_side(tables=3, rows=50)
    _consume(grid, tables, spool_limit=1 << 30, max_scan_area=50 * 12)
    assert len(grid.scans) == 1


def test_an_unreadable_spool_resumes_with_a_dedicated_scan_without_duplicates(monkeypatch):
    reads = 0
    original_read = router._Spool.read

    def failing_read(self, handle):
        nonlocal reads
        reads += 1
        if reads == 2:  # the first block replays, the second cannot be read back
            raise OSError(5, "Input/output error")
        return original_read(self, handle)

    monkeypatch.setattr(router._Spool, "read", failing_read)
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 128)
    grid, tables = _side_by_side(tables=4, rows=200)
    taken = _consume(grid, tables, spool_limit=1 << 30, spool_memory_limit=0)

    assert reads == 2
    for table in tables:
        assert taken[table.name] == _reference(grid, table)


def test_typical_sheets_never_touch_the_disk(monkeypatch):
    """The memory tier keeps the single scan working where TMPDIR is unusable."""

    def no_storage(*args, **kwargs):
        raise AssertionError("the temporary file must not be needed")

    monkeypatch.setattr(router.tempfile, "TemporaryFile", no_storage)
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 64)
    grid, tables = _side_by_side(tables=8)
    taken = _consume(grid, tables, spool_limit=1 << 30)

    assert len(grid.scans) == 1
    for table in tables:
        assert taken[table.name] == _reference(grid, table)


def test_memory_read_back_is_reused_by_later_tables(monkeypatch):
    """Replaying a table frees its blocks, so a memory tier smaller than the sum
    of all waiting rows still serves tables one after another without the disk."""
    peak_memory = written = 0
    original_write = router._Spool.write

    def write(self, data):
        nonlocal peak_memory, written
        result = original_write(self, data)
        written += len(data)
        peak_memory = max(peak_memory, self.in_memory)
        return result

    def no_storage(*args, **kwargs):
        raise AssertionError("the temporary file must not be needed")

    monkeypatch.setattr(router._Spool, "write", write)
    monkeypatch.setattr(router.tempfile, "TemporaryFile", no_storage)
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 64)
    # Staggered tables: each one's waiting rows are read back before the next
    # table's rows pile up, so memory is freed and reused in between.
    grid = _Grid([[(r * 100 + c) for c in range(12)] for r in range(300)])
    tables = [_Table(f"t{index}", 1 + index * 60, 120 + index * 60, 1 + index * 3, 2 + index * 3) for index in range(3)]
    _consume(grid, tables, spool_limit=1 << 30, spool_memory_limit=1 << 30)
    assert peak_memory < written  # memory was freed between tables

    needed, peak_memory = peak_memory, 0
    taken = _consume(grid, tables, spool_limit=1 << 30, spool_memory_limit=needed)

    assert len(grid.scans) == 1
    for table in tables:
        assert taken[table.name] == _reference(grid, table)


def test_rows_past_the_memory_tier_with_unusable_disk_fall_back(monkeypatch):
    def no_storage(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(router.tempfile, "TemporaryFile", no_storage)
    monkeypatch.setattr(router, "_SPOOL_BLOCK_BYTES", 64)
    grid, tables = _side_by_side(tables=4)
    taken = _consume(grid, tables, spool_limit=1 << 30, spool_memory_limit=1_000)

    assert len(grid.scans) == 1 + 3
    for table in tables:
        assert taken[table.name] == _reference(grid, table)
