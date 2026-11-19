"""Postgres implementation of :class:`FileFenceRepository` (#1430)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
from core.models.catalog import TERMINAL_TASK_STATES, FileFence, FileFenceKind
from core.ports.file_fence_repo import FileFenceRepository

_TERMINAL_JOB_STATUSES = sorted(state.value for state in TERMINAL_TASK_STATES)

# Takes a free fence, renews our own, or takes one whose holder is gone; returns
# nothing when another holder keeps it. Who that holder is comes from a second
# statement: a fallback branch in this one would read the snapshot the statement
# started with, so after waiting on a concurrent take-over it would name the
# holder that was just replaced.
_ACQUIRE_SQL = """
WITH taken AS (
    INSERT INTO file_fences (partition_name, file_id, holder_kind, holder_id, expires_at)
    VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5))
    ON CONFLICT (partition_name, file_id) DO UPDATE
       SET holder_kind = EXCLUDED.holder_kind,
           holder_id = EXCLUDED.holder_id,
           acquired_at = CASE
               WHEN file_fences.holder_id = EXCLUDED.holder_id THEN file_fences.acquired_at
               ELSE now()
           END,
           expires_at = EXCLUDED.expires_at
     WHERE file_fences.holder_id = EXCLUDED.holder_id
        OR file_fences.expires_at <= now()
        OR (
            file_fences.holder_kind = 'index'
            AND EXISTS (
                SELECT 1 FROM jobs
                 WHERE jobs.id = file_fences.holder_id
                   AND jobs.status = ANY($6::text[])
            )
        )
    RETURNING partition_name, file_id, holder_kind, holder_id, acquired_at, expires_at
)
SELECT partition_name, file_id, holder_kind, holder_id, acquired_at, expires_at FROM taken
"""

_HOLDER_SQL = """
SELECT partition_name, file_id, holder_kind, holder_id, acquired_at, expires_at
  FROM file_fences
 WHERE partition_name = $1 AND file_id = $2
"""

# The fence while its lease runs. Unlike _ACQUIRE_SQL, a terminal job does not
# free an index fence here: a job can turn FAILED while its worker still writes
# (fail_orphaned_jobs at startup), and only the lease is kept fresh by the
# holder that is actually running.
_LIVE_SQL = """
SELECT partition_name, file_id, holder_kind, holder_id, acquired_at, expires_at
  FROM file_fences
 WHERE partition_name = $1 AND file_id = $2
   AND expires_at > now()
"""

# Each retry means the holder released the fence between the two statements;
# a handful bounds a pathological loop without ever being reached in practice.
_ACQUIRE_ATTEMPTS = 5


class PgFileFenceRepository(FileFenceRepository):
    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    async def acquire(
        self,
        *,
        partition: str,
        file_id: str,
        kind: FileFenceKind,
        holder_id: str,
        ttl_seconds: float,
    ) -> FileFence:
        for _ in range(_ACQUIRE_ATTEMPTS):
            row = await self.pool.fetchrow(
                _ACQUIRE_SQL,
                partition,
                file_id,
                kind.value,
                holder_id,
                float(ttl_seconds),
                _TERMINAL_JOB_STATUSES,
            )
            if row is None:
                row = await self.pool.fetchrow(_HOLDER_SQL, partition, file_id)
            if row is not None:
                return _row_to_fence(row)
        raise RuntimeError(f"Could not read the fence of file {file_id!r} in partition {partition!r}")

    async def take_over(
        self,
        *,
        partition: str,
        file_id: str,
        kind: FileFenceKind,
        holder_id: str,
        expected_holder_id: str,
        ttl_seconds: float,
    ) -> bool:
        taken = await self.pool.fetchval(
            """
            UPDATE file_fences
               SET holder_kind = $3,
                   holder_id = $4,
                   acquired_at = now(),
                   expires_at = now() + make_interval(secs => $6)
             WHERE partition_name = $1 AND file_id = $2 AND holder_id = $5
            RETURNING 1
            """,
            partition,
            file_id,
            kind.value,
            holder_id,
            expected_holder_id,
            float(ttl_seconds),
        )
        return bool(taken)

    async def renew(self, *, partition: str, file_id: str, holder_id: str, ttl_seconds: float) -> bool:
        renewed = await self.pool.fetchval(
            """
            UPDATE file_fences
               SET expires_at = now() + make_interval(secs => $4)
             WHERE partition_name = $1 AND file_id = $2 AND holder_id = $3
            RETURNING 1
            """,
            partition,
            file_id,
            holder_id,
            float(ttl_seconds),
        )
        return bool(renewed)

    async def renew_all(self, fences: list[tuple[str, str, str]], *, ttl_seconds: float) -> int:
        if not fences:
            return 0
        partitions, file_ids, holder_ids = (list(column) for column in zip(*fences, strict=True))
        renewed = await self.pool.fetchval(
            """
            WITH lockable AS (
                SELECT f.partition_name, f.file_id
                  FROM file_fences f
                  JOIN unnest($1::text[], $2::text[], $3::text[]) AS held(partition_name, file_id, holder_id)
                    ON f.partition_name = held.partition_name
                   AND f.file_id = held.file_id
                   AND f.holder_id = held.holder_id
                   FOR NO KEY UPDATE OF f SKIP LOCKED
            ),
            renewed AS (
                UPDATE file_fences
                   SET expires_at = now() + make_interval(secs => $4)
                  FROM lockable
                 WHERE file_fences.partition_name = lockable.partition_name
                   AND file_fences.file_id = lockable.file_id
                RETURNING 1
            )
            SELECT count(*) FROM renewed
            """,
            partitions,
            file_ids,
            holder_ids,
            float(ttl_seconds),
        )
        return int(renewed)

    async def release(self, *, partition: str, file_id: str, holder_id: str) -> bool:
        released = await self.pool.fetchval(
            """
            DELETE FROM file_fences
             WHERE partition_name = $1 AND file_id = $2 AND holder_id = $3
            RETURNING 1
            """,
            partition,
            file_id,
            holder_id,
        )
        return bool(released)

    async def get(self, *, partition: str, file_id: str) -> FileFence | None:
        row = await self.pool.fetchrow(
            """
            SELECT partition_name, file_id, holder_kind, holder_id, acquired_at, expires_at
              FROM file_fences
             WHERE partition_name = $1 AND file_id = $2
            """,
            partition,
            file_id,
        )
        return _row_to_fence(row) if row is not None else None

    async def get_live(self, *, partition: str, file_id: str) -> FileFence | None:
        row = await self.pool.fetchrow(_LIVE_SQL, partition, file_id)
        return _row_to_fence(row) if row is not None else None


def _row_to_fence(row: Any) -> FileFence:
    return FileFence(
        partition=row["partition_name"],
        file_id=row["file_id"],
        kind=FileFenceKind(row["holder_kind"]),
        holder_id=row["holder_id"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
    )
