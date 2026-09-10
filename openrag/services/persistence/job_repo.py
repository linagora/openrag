"""asyncpg-backed :class:`JobRepository`.

The durable record of indexing job state. The actor bounds its own retention,
so it stops being able to answer for a task once it evicts it, and a restart
takes the rest with it. These rows are what survives both, and the queue views
union them with whatever the actor still holds: history comes from here, live
sub-state from the actor that is writing it.

Writes stay best-effort by design: a Postgres blip degrades history, it does
not fail indexing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from core.models.catalog import TERMINAL_TASK_STATES, IndexationJob
from core.ports.job_repo import JobRepository

if TYPE_CHECKING:
    import asyncpg

_COLUMNS = "id, partition, file_id, user_id, status, error, created_at, updated_at, started_at, completed_at"
_TERMINAL_STATUSES = sorted(state.value for state in TERMINAL_TASK_STATES)
_MAX_ERROR_CHARS = 8_000


class PgJobRepository(JobRepository):
    """Store one row per indexing task, keyed by task id."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    @staticmethod
    def _row_to_job(row: asyncpg.Record) -> IndexationJob:
        return IndexationJob(**dict(row))

    async def upsert_job(self, job: IndexationJob) -> IndexationJob:
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO jobs (id, partition, file_id, user_id, status, error, started_at, completed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                -- A settled job never reopens, mirroring TaskStateManager.
                status = CASE
                    WHEN jobs.status = ANY($9::text[]) THEN jobs.status
                    ELSE EXCLUDED.status
                END,
                -- The outcome is decided by whichever write settled the row, and
                -- the three fields that describe it move together. Freezing the
                -- status alone lets a FAILED write that lost the cancel race
                -- staple its traceback onto a row reading CANCELLED.
                error = CASE
                    WHEN jobs.status = ANY($9::text[]) THEN jobs.error
                    ELSE COALESCE(EXCLUDED.error, jobs.error)
                END,
                completed_at = CASE
                    WHEN jobs.status = ANY($9::text[]) THEN jobs.completed_at
                    ELSE COALESCE(jobs.completed_at, EXCLUDED.completed_at)
                END,
                -- First stamp wins: a retried transition must not restart the
                -- clock queue wait is measured against.
                started_at = COALESCE(jobs.started_at, EXCLUDED.started_at),
                file_id = COALESCE(EXCLUDED.file_id, jobs.file_id),
                user_id = COALESCE(EXCLUDED.user_id, jobs.user_id),
                updated_at = now()
            RETURNING {_COLUMNS}
            """,
            job.id,
            job.partition,
            job.file_id,
            job.user_id,
            job.status.value,
            job.error[:_MAX_ERROR_CHARS] if job.error else None,
            job.started_at,
            job.completed_at,
            _TERMINAL_STATUSES,
        )
        return self._row_to_job(row)

    async def get_job(self, job_id: str) -> IndexationJob | None:
        row = await self.pool.fetchrow(f"SELECT {_COLUMNS} FROM jobs WHERE id = $1", job_id)
        return self._row_to_job(row) if row is not None else None

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        user_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[IndexationJob]:
        rows = await self.pool.fetch(
            f"""
            SELECT {_COLUMNS} FROM jobs
            WHERE ($1::text IS NULL OR status = $1)
              AND ($2::int IS NULL OR user_id = $2)
            ORDER BY created_at DESC
            OFFSET $3 LIMIT $4
            """,
            status,
            user_id,
            max(0, offset),
            max(1, limit),
        )
        return [self._row_to_job(row) for row in rows]

    async def fail_orphaned_jobs(self, *, active_ids: list[str], error: str, before: datetime) -> int:
        return await self.pool.fetchval(
            """
            WITH failed AS (
                UPDATE jobs
                SET status = 'FAILED', error = $1, completed_at = now(), updated_at = now()
                WHERE status <> ALL($2::text[])
                  AND id <> ALL($3::text[])
                  AND updated_at < $4
                RETURNING 1
            )
            SELECT COUNT(*)::int FROM failed
            """,
            error[:_MAX_ERROR_CHARS],
            _TERMINAL_STATUSES,
            list(active_ids),
            before,
        )

    async def purge_terminal_jobs(self, *, older_than: datetime) -> int:
        return await self.pool.fetchval(
            """
            WITH purged AS (
                DELETE FROM jobs
                -- Matches ix_jobs_settled_at. A row whose terminal write
                -- raced a failure has no completed_at and ages out on created_at.
                WHERE status = ANY($1::text[]) AND COALESCE(completed_at, created_at) < $2
                RETURNING 1
            )
            SELECT COUNT(*)::int FROM purged
            """,
            _TERMINAL_STATUSES,
            older_than,
        )


__all__ = ["PgJobRepository"]
