"""Postgres implementation of :class:`FileFenceRepository` (#1430)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import asyncpg
from core.models.catalog import TERMINAL_TASK_STATES, FileFence, FileFenceKind
from core.ports.file_fence_repo import FileFenceRepository

_TERMINAL_JOB_STATUSES = sorted(state.value for state in TERMINAL_TASK_STATES)

_INSERT_SQL = """
INSERT INTO file_fences (partition_name, file_id, holder_kind, holder_id, expires_at)
VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5))
ON CONFLICT (partition_name, file_id) DO NOTHING
RETURNING partition_name, file_id, holder_kind, holder_id, acquired_at, expires_at
"""

_UPDATE_SQL = """
UPDATE file_fences
   SET holder_kind = $3,
       holder_id = $4,
       acquired_at = CASE WHEN holder_id = $4 THEN acquired_at ELSE now() END,
       expires_at = now() + make_interval(secs => $5)
 WHERE partition_name = $1 AND file_id = $2
   AND (
       holder_id = $4
       OR (expires_at <= now() AND NOT EXISTS (
           SELECT 1 FROM jobs
            WHERE jobs.id = file_fences.holder_id
              AND file_fences.holder_kind = 'index'
              AND jobs.lane IS NOT NULL
              AND jobs.status IN ('QUEUED', 'SERIALIZING')
       ))
       OR (holder_kind = 'index' AND EXISTS (
           SELECT 1 FROM jobs
            WHERE jobs.id = file_fences.holder_id AND jobs.status = ANY($6::text[])
       ))
   )
RETURNING partition_name, file_id, holder_kind, holder_id, acquired_at, expires_at
"""

_HOLDER_SQL = """
SELECT partition_name, file_id, holder_kind, holder_id, acquired_at, expires_at
  FROM file_fences
 WHERE partition_name = $1 AND file_id = $2
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
        _connection: asyncpg.Connection | None = None,
    ) -> FileFence:
        """Use the caller's transaction when acquiring a fence for queue admission."""
        args = (partition, file_id, kind.value, holder_id, float(ttl_seconds))
        if _connection is not None:
            return await self._acquire_in_transaction(_connection, args)
        async with self.pool.acquire() as connection, connection.transaction(isolation="read_committed"):
            return await self._acquire_in_transaction(connection, args)

    async def _acquire_in_transaction(
        self, connection: asyncpg.Connection, args: tuple[str, str, str, str, float]
    ) -> FileFence:
        for _ in range(_ACQUIRE_ATTEMPTS):
            inserted = await connection.fetchrow(_INSERT_SQL, *args)
            if inserted is not None:
                return _row_to_fence(inserted)
            holder = await connection.fetchrow(_HOLDER_SQL + " FOR UPDATE", *args[:2])
            if holder is not None:
                # Read job state after locking the fence so a concurrent enqueue's
                # committed job is visible, even if the fence lock made us wait.
                updated = await connection.fetchrow(_UPDATE_SQL, *args, _TERMINAL_JOB_STATUSES)
                return _row_to_fence(holder if updated is None else updated)
        partition, file_id = args[:2]
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


def _row_to_fence(row: Any) -> FileFence:
    return FileFence(
        partition=row["partition_name"],
        file_id=row["file_id"],
        kind=FileFenceKind(row["holder_kind"]),
        holder_id=row["holder_id"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
    )
