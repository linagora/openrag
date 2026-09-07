"""asyncpg-backed :class:`JobRepository`.

Durable mirror of the in-memory ``TaskStateManager``. The actor stays the hot
path while it is alive; these rows are what remains after a restart, so job
history stays observable and orphaned work is recoverable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from core.models.catalog import TERMINAL_TASK_STATES, IndexationJob
from core.ports.job_repo import JobRepository

if TYPE_CHECKING:
    import asyncpg

_COLUMNS = "id, partition, file_id, user_id, status, error, created_at, updated_at, finished_at"
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
            INSERT INTO jobs (id, partition, file_id, user_id, status, error, finished_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE SET
                -- A settled job never reopens, mirroring TaskStateManager.
                status = CASE
                    WHEN jobs.status = ANY($8::text[]) THEN jobs.status
                    ELSE EXCLUDED.status
                END,
                file_id = COALESCE(EXCLUDED.file_id, jobs.file_id),
                user_id = COALESCE(EXCLUDED.user_id, jobs.user_id),
                error = COALESCE(EXCLUDED.error, jobs.error),
                finished_at = COALESCE(jobs.finished_at, EXCLUDED.finished_at),
                updated_at = now()
            RETURNING {_COLUMNS}
            """,
            job.id,
            job.partition,
            job.file_id,
            job.user_id,
            job.status.value,
            job.error[:_MAX_ERROR_CHARS] if job.error else None,
            job.finished_at,
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
              AND ($2::bigint IS NULL OR user_id = $2)
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
                SET status = 'FAILED', error = $1, finished_at = now(), updated_at = now()
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
                WHERE status = ANY($1::text[]) AND COALESCE(finished_at, updated_at) < $2
                RETURNING 1
            )
            SELECT COUNT(*)::int FROM purged
            """,
            _TERMINAL_STATUSES,
            older_than,
        )


__all__ = ["PgJobRepository"]
