"""Unit tests for :class:`PgJobRepository`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from core.models.catalog import DocumentStatus, IndexationJob
from services.persistence.job_repo import PgJobRepository

_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _row(**kwargs):
    base = {
        "id": "task-1",
        "partition": "tenant-a",
        "file_id": "file-1",
        "user_id": 7,
        "status": "QUEUED",
        "error": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "started_at": None,
        "completed_at": None,
    }
    base.update(kwargs)
    return base


class _FakePool:
    def __init__(self, *, fetchrow=None, fetch=None, fetchval=None):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchrow = fetchrow
        self._fetch = fetch or []
        self._fetchval = fetchval

    async def fetchrow(self, query, *params):
        self.calls.append((query, params))
        return self._fetchrow

    async def fetch(self, query, *params):
        self.calls.append((query, params))
        return self._fetch

    async def fetchval(self, query, *params):
        self.calls.append((query, params))
        return self._fetchval


def _repo(pool):
    return PgJobRepository(lambda: pool)


@pytest.mark.asyncio
async def test_upsert_job_writes_the_task_row_and_maps_it_back():
    pool = _FakePool(fetchrow=_row(status="SERIALIZING"))
    repo = _repo(pool)

    job = await repo.upsert_job(
        IndexationJob(
            id="task-1",
            status=DocumentStatus.SERIALIZING,
            partition="tenant-a",
            file_id="file-1",
            user_id=7,
        )
    )

    query, params = pool.calls[0]
    assert params[:6] == ("task-1", "tenant-a", "file-1", 7, "SERIALIZING", None)
    assert job.status is DocumentStatus.SERIALIZING
    assert job.file_id == "file-1"


@pytest.mark.asyncio
async def test_upsert_job_keeps_settled_states_and_bounds_the_error():
    pool = _FakePool(fetchrow=_row(status="FAILED", error="boom"))
    repo = _repo(pool)

    await repo.upsert_job(
        IndexationJob(id="task-1", status=DocumentStatus.FAILED, partition="tenant-a", error="x" * 20_000)
    )

    query, params = pool.calls[0]
    # A settled row never reopens, mirroring the TaskStateManager guard.
    assert "WHEN jobs.status = ANY($9::text[]) THEN jobs.status" in query
    assert sorted(params[8]) == ["CANCELLED", "COMPLETED", "FAILED"]
    assert len(params[5]) == 8_000


@pytest.mark.asyncio
async def test_upsert_job_freezes_the_outcome_fields_together_on_a_settled_row():
    """A FAILED write that lost the cancel race must not mark a CANCELLED row.

    Freezing ``status`` alone leaves ``error`` and ``completed_at`` writable, so
    the row reads CANCELLED while carrying the loser's traceback.
    """
    pool = _FakePool(fetchrow=_row(status="CANCELLED"))
    repo = _repo(pool)

    await repo.upsert_job(
        IndexationJob(
            id="task-1",
            status=DocumentStatus.FAILED,
            partition="tenant-a",
            error="late traceback",
            completed_at=_NOW,
        )
    )

    query, _params = pool.calls[0]
    compact = " ".join(query.split())
    settled = "jobs.status = ANY($9::text[])"
    for field, frozen in (("status", "jobs.status"), ("error", "jobs.error"), ("completed_at", "jobs.completed_at")):
        assert f"{field} = CASE WHEN {settled} THEN {frozen}" in compact, field


@pytest.mark.asyncio
async def test_upsert_job_keeps_the_first_started_at():
    """Queue wait is measured against the first stamp, so a retry cannot move it."""
    pool = _FakePool(fetchrow=_row(status="SERIALIZING", started_at=_NOW))
    repo = _repo(pool)

    job = await repo.upsert_job(
        IndexationJob(
            id="task-1",
            status=DocumentStatus.SERIALIZING,
            partition="tenant-a",
            started_at=_NOW,
        )
    )

    query, params = pool.calls[0]
    assert "started_at = COALESCE(jobs.started_at, EXCLUDED.started_at)" in query
    assert params[6] == _NOW
    assert job.started_at == _NOW


@pytest.mark.asyncio
async def test_get_job_returns_none_when_absent():
    assert await _repo(_FakePool(fetchrow=None)).get_job("nope") is None


@pytest.mark.asyncio
async def test_list_jobs_filters_by_status_and_user():
    pool = _FakePool(fetch=[_row(), _row(id="task-2")])
    repo = _repo(pool)

    jobs = await repo.list_jobs(status="FAILED", user_id=7, offset=-5, limit=0)

    _query, params = pool.calls[0]
    assert params == ("FAILED", 7, 0, 1)
    assert [job.id for job in jobs] == ["task-1", "task-2"]


@pytest.mark.asyncio
async def test_fail_orphaned_jobs_skips_settled_rows_and_live_tasks():
    pool = _FakePool(fetchval=3)
    repo = _repo(pool)

    cutoff = _NOW - timedelta(minutes=5)
    assert await repo.fail_orphaned_jobs(active_ids=["task-9"], error="restart", before=cutoff) == 3

    query, params = pool.calls[0]
    assert "status <> ALL($2::text[])" in query
    assert "id <> ALL($3::text[])" in query
    # A row written moments ago belongs to a dispatch still in flight.
    assert "updated_at < $4" in query
    assert "completed_at = now()" in query
    assert params[2] == ["task-9"]
    assert params[3] == cutoff


@pytest.mark.asyncio
async def test_purge_terminal_jobs_only_removes_settled_rows():
    pool = _FakePool(fetchval=12)
    repo = _repo(pool)
    cutoff = _NOW - timedelta(days=30)

    assert await repo.purge_terminal_jobs(older_than=cutoff) == 12

    query, params = pool.calls[0]
    assert "status = ANY($1::text[])" in query
    # Must match ix_jobs_settled_at, or the sweep cannot use it.
    assert "COALESCE(completed_at, created_at) < $2" in query
    assert params[1] == cutoff
