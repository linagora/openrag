"""Unit tests for the durable :class:`PgJobRepository` (issue #660).

The SQL is exercised against a fake asyncpg pool: these assert the shape of
the statements and the row -> model mapping. End-to-end behaviour against a
real Postgres lives in ``tests/integration/repos/``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.models.catalog import DocumentStatus, IndexationJob


class _FakePool:
    def __init__(self, row=None, rows=None, val=None):
        self.executed: list[tuple[str, tuple]] = []
        self._row = row
        self._rows = rows or []
        self._val = val

    async def fetchrow(self, query: str, *params):
        self.executed.append((query, params))
        return self._row

    async def fetch(self, query: str, *params):
        self.executed.append((query, params))
        return self._rows

    async def fetchval(self, query: str, *params):
        self.executed.append((query, params))
        return self._val

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "DELETE 3"


def _db_row(**overrides) -> dict:
    row = {
        "id": "task-1",
        "status": "QUEUED",
        "partition": "tenant-a",
        "file_id": "file-1",
        "user_id": 42,
        "job_metadata": {"filename": "report.pdf"},
        "error": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "started_at": None,
        "completed_at": None,
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _repo(pool):
    from services.persistence.job_repo import PgJobRepository

    return PgJobRepository(pool_getter=lambda: pool)


async def test_create_job_inserts_row_and_returns_model():
    pool = _FakePool(row=_db_row())
    job = await _repo(pool).create_job(
        IndexationJob(
            id="task-1",
            status=DocumentStatus.QUEUED,
            partition="tenant-a",
            file_id="file-1",
            user_id=42,
            job_metadata={"filename": "report.pdf"},
        )
    )

    query, params = pool.executed[0]
    assert "INSERT INTO jobs" in query
    assert params[0] == "task-1"
    assert params[1] == "QUEUED"
    assert isinstance(job, IndexationJob)
    assert job.id == "task-1"
    assert job.status is DocumentStatus.QUEUED
    assert job.job_metadata == {"filename": "report.pdf"}


async def test_create_job_is_idempotent_on_redispatch():
    pool = _FakePool(row=_db_row())
    await _repo(pool).create_job(IndexationJob(id="task-1"))

    query, _ = pool.executed[0]
    assert "ON CONFLICT (id) DO" in query


async def test_get_job_returns_none_when_missing():
    pool = _FakePool(row=None)
    assert await _repo(pool).get_job("nope") is None


async def test_update_job_sets_only_allowlisted_fields():
    pool = _FakePool(row=_db_row(status="COMPLETED"))
    job = await _repo(pool).update_job("task-1", status=DocumentStatus.COMPLETED, bogus="x")

    query, params = pool.executed[0]
    assert "UPDATE jobs" in query
    assert "bogus" not in query
    assert "QUEUED" not in params
    assert job.status is DocumentStatus.COMPLETED


async def test_update_job_upper_cases_a_lower_case_status():
    """A lower-case status must never reach the ``ck_jobs_status`` CHECK.

    ``update_job`` takes ``**fields`` from the worker path, so unlike
    ``create_job`` (whose ``IndexationJob.status`` pydantic validates to a
    ``DocumentStatus``) it can receive a plain string. Every durable write is
    best-effort, so a CHECK violation here would be swallowed and the job would
    silently freeze at its previous status — permanently, if the dropped write
    was the terminal one.
    """
    pool = _FakePool(row=_db_row(status="COMPLETED"))
    await _repo(pool).update_job("task-1", status="completed")

    _, params = pool.executed[0]
    assert "COMPLETED" in params
    assert "completed" not in params


async def test_update_job_truncates_error_text():
    pool = _FakePool(row=_db_row(status="FAILED", error="x"))
    await _repo(pool).update_job("task-1", status=DocumentStatus.FAILED, error="y" * 50_000)

    _, params = pool.executed[0]
    stored = next(p for p in params if isinstance(p, str) and p.startswith("["))
    assert len(stored) < 10_000
    assert "truncated" in stored


async def test_mark_failed_if_not_cancelled_guards_on_status_in_sql():
    """The CANCELLED check must be part of the UPDATE, not a read-then-write.

    Arbitrating in the statement is what lets a worker whose state actor has
    forgotten the task still reach a terminal row (#660): the actor can no
    longer veto the write, and a concurrent cancel is still respected.
    """
    pool = _FakePool(row={"id": "task-1"})
    assert await _repo(pool).mark_failed_if_not_cancelled(
        "task-1", error="boom", completed_at=datetime(2026, 1, 1, tzinfo=UTC)
    )

    query, params = pool.executed[0]
    assert "UPDATE jobs" in query
    assert "status <> 'CANCELLED'" in query
    assert "task-1" in params


async def test_mark_failed_if_not_cancelled_reports_a_cancelled_row():
    pool = _FakePool(row=None)  # WHERE matched nothing: already CANCELLED, or gone

    assert not await _repo(pool).mark_failed_if_not_cancelled(
        "task-1", error="boom", completed_at=datetime(2026, 1, 1, tzinfo=UTC)
    )


async def test_mark_failed_if_not_cancelled_truncates_error_text():
    pool = _FakePool(row={"id": "task-1"})
    await _repo(pool).mark_failed_if_not_cancelled(
        "task-1", error="y" * 50_000, completed_at=datetime(2026, 1, 1, tzinfo=UTC)
    )

    _, params = pool.executed[0]
    stored = next(p for p in params if isinstance(p, str) and p.startswith("["))
    assert len(stored) < 10_000
    assert "truncated" in stored


async def test_update_job_with_no_known_fields_is_a_noop_read():
    pool = _FakePool(row=_db_row())
    job = await _repo(pool).update_job("task-1", bogus="x")

    query, _ = pool.executed[0]
    assert query.strip().startswith("SELECT")
    assert job.id == "task-1"


async def test_list_jobs_filters_by_user_and_status():
    pool = _FakePool(rows=[_db_row()])
    jobs = await _repo(pool).list_jobs(status="FAILED", user_id=42, offset=5, limit=10)

    query, params = pool.executed[0]
    assert "FROM jobs" in query
    assert ["FAILED"] in params
    assert 42 in params
    assert 10 in params and 5 in params
    assert len(jobs) == 1


async def test_list_jobs_active_expands_to_the_non_terminal_states():
    pool = _FakePool(rows=[])
    await _repo(pool).list_jobs(status="active")

    _, params = pool.executed[0]
    assert ["QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"] in params


async def test_list_jobs_status_match_is_case_insensitive():
    pool = _FakePool(rows=[])
    await _repo(pool).list_jobs(status="failed")

    _, params = pool.executed[0]
    assert ["FAILED"] in params


async def test_count_by_status_returns_mapping():
    pool = _FakePool(rows=[{"status": "COMPLETED", "count": 3}, {"status": "FAILED", "count": 1}])
    counts = await _repo(pool).count_by_status()

    assert counts == {"COMPLETED": 3, "FAILED": 1}


async def test_purge_terminal_jobs_deletes_aged_and_overflow_rows():
    pool = _FakePool(val=3)
    purged = await _repo(pool).purge_terminal_jobs(older_than_seconds=3600, keep_last=100)

    assert purged == 3
    query, params = pool.executed[0]
    assert "DELETE FROM jobs" in query
    assert ["COMPLETED", "FAILED", "CANCELLED"] in params
    assert 3600 in params
    assert 100 in params


async def test_purge_terminal_jobs_rejects_negative_bounds():
    with pytest.raises(ValueError):
        await _repo(_FakePool()).purge_terminal_jobs(older_than_seconds=-1, keep_last=10)


async def test_an_unwritable_status_is_rejected_before_it_reaches_sql():
    """A status outside the CHECK must fail loudly here, not silently in SQL.

    Every durable write is best-effort, so a ``CheckViolationError`` from
    ``ck_jobs_status`` is swallowed by the caller and the job freezes at its
    previous status — permanently, if the dropped write was the terminal one.
    Casing is not the only way to build such a value: ``update_job`` is untyped,
    and ``str(None).upper()`` is ``"NONE"``.
    """
    pool = _FakePool(row=_db_row())
    for bad in (None, "bogus", "RUNNING"):
        with pytest.raises(ValueError, match="unknown job status"):
            await _repo(pool).update_job("task-1", status=bad)

    assert pool.executed == [], "a rejected status must not reach the database"


async def test_every_allowed_status_survives_the_guard():
    """The other half: the seven states the CHECK allows must all pass."""
    for state in DocumentStatus:
        pool = _FakePool(row=_db_row(status=state.value))
        assert await _repo(pool).update_job("task-1", status=state) is not None
        assert await _repo(pool).update_job("task-1", status=state.value.lower()) is not None
