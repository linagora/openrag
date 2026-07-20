"""asyncpg-backed :class:`JobRepository` — durable indexation job state.

Replaces the Phase 7A.2 stub (issue #660). Job state used to live *only* in the
detached ``TaskStateManager`` Ray actor: unbounded, volatile and invisible to
operators, so any restart mid-batch made the in-flight work unobservable and
un-cancellable. The ``jobs`` table (one row per dispatched task, see
``schema.py``) is now the source of truth; the actor is a hot cache in front of
it.

Two invariants shape this module:

* **Bounded rows.** Terminal jobs are evicted by :meth:`purge_terminal_jobs`
  and stored errors are truncated at write time — otherwise the durable store
  would just reproduce the in-memory leak on disk.
* **Column allowlisting.** :meth:`update_job` takes ``**fields`` from callers
  in the worker path; only known columns reach the SQL string.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.models.catalog import IndexationJob
from core.ports.job_repo import ACTIVE_JOB_STATES, TERMINAL_JOB_STATES, JobRepository
from core.utils.text import truncate_error_text

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


_COLUMNS = (
    "id",
    "status",
    "partition",
    "file_id",
    "user_id",
    "job_metadata",
    "error",
    "created_at",
    "started_at",
    "completed_at",
    "updated_at",
)

# Columns ``update_job(**fields)`` may write. ``id`` and ``created_at`` are
# immutable identity; ``updated_at`` is stamped by the repository itself.
_UPDATABLE_COLUMNS = frozenset(
    {
        "status",
        "partition",
        "file_id",
        "user_id",
        "job_metadata",
        "error",
        "started_at",
        "completed_at",
    }
)

_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM jobs"


class PgJobRepository(JobRepository):
    """Store indexation job lifecycle state in Postgres."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def create_job(self, job: IndexationJob) -> IndexationJob:
        row = await self.pool.fetchrow(
            f"""
            INSERT INTO jobs (id, status, partition, file_id, user_id, job_metadata, error,
                              created_at, started_at, completed_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (id) DO UPDATE
                SET status = EXCLUDED.status,
                    updated_at = now()
            RETURNING {", ".join(_COLUMNS)}
            """,
            job.id,
            _status_value(job.status),
            job.partition,
            job.file_id,
            job.user_id,
            dict(job.job_metadata),
            truncate_error_text(job.error),
            job.created_at,
            job.started_at,
            job.completed_at,
            job.updated_at,
        )
        created = _row_to_job(row)
        if created is None:  # pragma: no cover - RETURNING always yields the upserted row
            raise RuntimeError(f"jobs upsert returned no row for job_id={job.id!r}")
        return created

    async def update_job(self, job_id: str, **fields: Any) -> IndexationJob | None:
        """Patch a job row; unknown keys are ignored rather than rejected.

        Callers live on the indexing hot path and pass whatever they know about
        the transition, so an unexpected key must not raise there — the
        allowlist silently drops it. With nothing left to write this degrades to
        a plain read, which keeps ``update_job`` total (it always returns the
        current row, or ``None`` if the job is gone).
        """
        updates = {key: value for key, value in fields.items() if key in _UPDATABLE_COLUMNS}
        if "error" in updates:
            updates["error"] = truncate_error_text(updates["error"])
        if "status" in updates:
            updates["status"] = _status_value(updates["status"])
        if "job_metadata" in updates:
            updates["job_metadata"] = dict(updates["job_metadata"] or {})

        if not updates:
            return await self.get_job(job_id)

        assignments = [f"{column} = ${i}" for i, column in enumerate(updates, start=2)]
        assignments.append("updated_at = now()")

        # A cancellation is sticky here for the same reason it is sticky in the
        # hot cache (``TaskStateManager.set_state``): once the user has asked
        # for a cancel and been told it landed, no later write may walk it back.
        # Without this guard a *winning* cancel — one that claims a task still
        # SERIALIZING — is clobbered by the worker's own in-flight lifecycle
        # write, because both are blind UPDATEs and Postgres is free to order
        # them either way. The COMPLETED case leaves the actor and the table
        # disagreeing forever; the SERIALIZING case is worse, stranding the row
        # non-terminal after ``ray.cancel`` has already killed the only writer
        # that could have finished it — and ``purge_terminal_jobs`` sweeps
        # terminal rows only, so it never ages out.
        # ``mark_failed_if_not_cancelled`` already arbitrates this in SQL; this
        # extends the same rule to the writes that lacked it.
        guard_cancelled = updates.get("status") not in (None, "CANCELLED")
        where = "id = $1" + (" AND status <> 'CANCELLED'" if guard_cancelled else "")

        row = await self.pool.fetchrow(
            f"""
            UPDATE jobs
            SET {", ".join(assignments)}
            WHERE {where}
            RETURNING {", ".join(_COLUMNS)}
            """,
            job_id,
            *updates.values(),
        )
        if row is None and guard_cancelled:
            # The guard declined the write, or the job is gone. Re-read so the
            # caller still gets the truth: returning ``None`` here would report
            # a cancelled job as a missing one.
            return await self.get_job(job_id)
        return _row_to_job(row)

    async def mark_failed_if_not_cancelled(self, job_id: str, *, error: str, completed_at: datetime) -> bool:
        """Move the row to FAILED unless a cancellation already claimed it.

        The guard is part of the UPDATE, so a concurrent cancel either lands
        before this statement (and the WHERE drops it) or after (and it
        overwrites a FAILED row the user never saw). No row returned means the
        job was already CANCELLED, or is gone.
        """
        row = await self.pool.fetchrow(
            """
            UPDATE jobs
            SET status = 'FAILED',
                error = $2,
                completed_at = $3,
                updated_at = now()
            WHERE id = $1
              AND status <> 'CANCELLED'
            RETURNING id
            """,
            job_id,
            truncate_error_text(error),
            completed_at,
        )
        return row is not None

    async def purge_terminal_jobs(self, *, older_than_seconds: int, keep_last: int) -> int:
        if older_than_seconds < 0 or keep_last < 0:
            raise ValueError("older_than_seconds and keep_last must be non-negative")

        # One statement for both bounds: age evicts the long tail on a busy
        # deployment, ``keep_last`` caps the table on a deployment that indexes
        # faster than the age window retires rows. ``completed_at IS NULL`` can
        # only happen for a row whose terminal write raced a schema/write error,
        # so age it out on created_at rather than leaking it forever.
        purged = await self.pool.fetchval(
            """
            WITH terminal AS (
                SELECT id,
                       COALESCE(completed_at, created_at) AS settled_at,
                       row_number() OVER (ORDER BY COALESCE(completed_at, created_at) DESC) AS recency
                FROM jobs
                WHERE status = ANY($1::text[])
            ),
            deleted AS (
                DELETE FROM jobs
                WHERE id IN (
                    SELECT id FROM terminal
                    WHERE settled_at < now() - make_interval(secs => $2::double precision)
                       OR recency > $3
                )
                RETURNING 1
            )
            SELECT COUNT(*)::int FROM deleted
            """,
            list(TERMINAL_JOB_STATES),
            older_than_seconds,
            keep_last,
        )
        return purged or 0

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_job(self, job_id: str) -> IndexationJob | None:
        row = await self.pool.fetchrow(f"{_SELECT} WHERE id = $1", job_id)
        return _row_to_job(row)

    async def list_jobs(
        self,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
        user_id: int | None = None,
    ) -> list[IndexationJob]:
        where = []
        params: list[Any] = []
        states = _expand_status(status)
        if states is not None:
            params.append(states)
            where.append(f"status = ANY(${len(params)}::text[])")
        if user_id is not None:
            params.append(user_id)
            where.append(f"user_id = ${len(params)}")

        clause = f" WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, limit))
        limit_param = len(params)
        params.append(max(0, offset))

        rows = await self.pool.fetch(
            f"{_SELECT}{clause} ORDER BY created_at DESC, id DESC LIMIT ${limit_param} OFFSET ${len(params)}",
            *params,
        )
        return [_row_to_job(row) for row in rows]

    async def count_by_status(self) -> dict[str, int]:
        rows = await self.pool.fetch("SELECT status, COUNT(*)::int AS count FROM jobs GROUP BY status")
        return {row["status"]: row["count"] for row in rows}


def _expand_status(status: str | None) -> list[str] | None:
    """Resolve a status filter to the concrete states it selects."""
    if status is None:
        return None
    if status.lower() == "active":
        return list(ACTIVE_JOB_STATES)
    return [status.upper()]


def _status_value(status: Any) -> str:
    """Normalize a status to the exact spelling the ``ck_jobs_status`` CHECK allows.

    Upper-cased for the same reason :func:`_expand_status` does it: a lower-case
    string would fail the CHECK, and every durable write is best-effort, so the
    violation would be swallowed and the job would silently freeze at its
    previous status — permanently, if the dropped write was the terminal one.
    Enum members already carry the right value; plain strings may not.
    """
    value = status.value if hasattr(status, "value") else str(status)
    return value.upper()


def _row_to_job(row: asyncpg.Record | None) -> IndexationJob | None:
    if row is None:
        return None
    data = dict(row)
    data["job_metadata"] = data.get("job_metadata") or {}
    return IndexationJob(**data)


__all__ = ["PgJobRepository"]
