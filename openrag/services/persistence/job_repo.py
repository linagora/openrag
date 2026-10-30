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

from collections.abc import Callable, Sequence
from datetime import datetime

import asyncpg
from core.models.catalog import TERMINAL_TASK_STATES, IndexationJob
from core.models.stage_progress import (
    DurableStageProgress,
    StageEventStatus,
    StageProgressCoverage,
    StageProgressEvent,
    StageProgressEventCursor,
    StageProgressEventPage,
    StageProgressSummary,
    StageProgressWriteResult,
)
from core.ports.job_repo import JobRepository

_COLUMNS = (
    "id, partition, file_id, filename, user_id, status, error, error_reason, degraded_stages, stage_tracking_version, "
    "created_at, updated_at, started_at, completed_at"
)
_TERMINAL_STATUSES = sorted(state.value for state in TERMINAL_TASK_STATES)
_MAX_ERROR_CHARS = 8_000
_MAX_STAGE_BATCH_EVENTS = 32
_MAX_STAGE_HISTORY_PAGE = 200
_STAGE_EVENT_COLUMNS = (
    "event_id, task_id, attempt, sequence, invocation_id, stage_id, stage_rank, "
    "status, occurred_at, duration_ms, failure_effect"
)
_STAGE_STATE_COLUMNS = (
    "task_id, attempt, latest_sequence, snapshot_revision, tracking_version, phase, "
    "current_stage_id, current_stage_rank, position_score, stage_started_at, current_invocation_id, "
    "interrupted_invocation_id, persisted_through_sequence, expected_final_sequence, known_loss, known_conflict, "
    "has_open_invocation, history_truncated"
)


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
            INSERT INTO jobs (
                id, partition, file_id, user_id, status, error, error_reason,
                started_at, completed_at, degraded_stages, filename, stage_tracking_version
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (id) DO UPDATE SET
                -- A settled job never reopens, mirroring TaskStateManager.
                status = CASE
                    WHEN jobs.status = ANY($13::text[]) THEN jobs.status
                    ELSE EXCLUDED.status
                END,
                -- The outcome is decided by whichever write settled the row, and
                -- the three fields that describe it move together. Freezing the
                -- status alone lets a FAILED write that lost the cancel race
                -- staple its traceback onto a row reading CANCELLED.
                error = CASE
                    WHEN jobs.status = ANY($13::text[]) THEN jobs.error
                    ELSE COALESCE(EXCLUDED.error, jobs.error)
                END,
                error_reason = CASE
                    WHEN jobs.status = ANY($13::text[]) THEN jobs.error_reason
                    ELSE COALESCE(EXCLUDED.error_reason, jobs.error_reason)
                END,
                completed_at = CASE
                    WHEN jobs.status = ANY($13::text[]) THEN jobs.completed_at
                    ELSE COALESCE(jobs.completed_at, EXCLUDED.completed_at)
                END,
                degraded_stages = CASE
                    WHEN jobs.status = ANY($13::text[]) THEN jobs.degraded_stages
                    ELSE EXCLUDED.degraded_stages
                END,
                -- Instrumentation evidence is monotonic: a positive v1
                -- observation upgrades a legacy marker, while a delayed old
                -- worker cannot erase a value already established.
                stage_tracking_version = CASE
                    WHEN jobs.stage_tracking_version = 1 OR EXCLUDED.stage_tracking_version = 1 THEN 1
                    ELSE COALESCE(jobs.stage_tracking_version, EXCLUDED.stage_tracking_version)
                END,
                -- First stamp wins: a retried transition must not restart the
                -- clock queue wait is measured against.
                started_at = COALESCE(jobs.started_at, EXCLUDED.started_at),
                file_id = COALESCE(EXCLUDED.file_id, jobs.file_id),
                filename = COALESCE(EXCLUDED.filename, jobs.filename),
                -- user_id is set once, at insert, and the foreign key owns it
                -- from then on. Deleting a user nulls it via ON DELETE SET NULL,
                -- and a later worker write still carries the old id, so taking
                -- EXCLUDED.user_id here would re-point the row at a user that no
                -- longer exists. Postgres rejects the whole upsert for that, the
                -- caller swallows it as a best-effort history write, and the
                -- terminal status is silently lost.
                updated_at = now()
            RETURNING {_COLUMNS}
            """,
            job.id,
            job.partition,
            job.file_id,
            job.user_id,
            job.status.value,
            job.error[:_MAX_ERROR_CHARS] if job.error else None,
            job.error_reason,
            job.started_at,
            job.completed_at,
            job.degraded_stages,
            job.filename,
            job.stage_tracking_version,
            _TERMINAL_STATUSES,
        )
        return self._row_to_job(row)

    async def get_job(self, job_id: str) -> IndexationJob | None:
        row = await self.pool.fetchrow(f"SELECT {_COLUMNS} FROM jobs WHERE id = $1", job_id)
        return self._row_to_job(row) if row is not None else None

    async def get_jobs(self, job_ids: list[str]) -> list[IndexationJob]:
        if not job_ids:
            return []
        rows = await self.pool.fetch(
            f"SELECT {_COLUMNS} FROM jobs WHERE id = ANY($1::text[])",
            job_ids,
        )
        return [self._row_to_job(row) for row in rows]

    async def list_jobs(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        offset: int = 0,
        limit: int | None = 50,
    ) -> list[IndexationJob]:
        """List the newest matching rows. Filtering happens here, before the
        limit, so a status query cannot be crowded out by newer rows."""
        rows = await self.pool.fetch(
            f"""
            SELECT {_COLUMNS} FROM jobs
            WHERE ($1::text[] IS NULL OR status = ANY($1::text[]))
              AND ($2::int IS NULL OR user_id = $2)
            ORDER BY created_at DESC
            OFFSET $3 LIMIT $4
            """,
            statuses,
            user_id,
            max(0, offset),
            max(1, limit) if limit is not None else None,
        )
        return [self._row_to_job(row) for row in rows]

    async def count_jobs(self) -> dict[str, int]:
        rows = await self.pool.fetch("SELECT status, COUNT(*)::int AS count FROM jobs GROUP BY status")
        return {row["status"]: int(row["count"]) for row in rows}

    async def get_job_states(
        self,
        *,
        statuses: list[str] | None = None,
        job_ids: list[str] | None = None,
    ) -> dict[str, str]:
        if statuses is None and job_ids is None:
            raise ValueError("get_job_states needs a status or task-ID filter")
        if (statuses is not None and not statuses) or (job_ids is not None and not job_ids):
            return {}
        rows = await self.pool.fetch(
            """
            SELECT id, status FROM jobs
            WHERE ($1::text[] IS NULL OR status = ANY($1::text[]))
              AND ($2::text[] IS NULL OR id = ANY($2::text[]))
            """,
            statuses,
            job_ids,
        )
        return {row["id"]: row["status"] for row in rows}

    async def write_stage_progress(
        self,
        *,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
    ) -> StageProgressWriteResult:
        """Persist a bounded event batch without creating parent job rows."""
        if len(events) > _MAX_STAGE_BATCH_EVENTS:
            raise ValueError(f"at most {_MAX_STAGE_BATCH_EVENTS} stage events may be written at once")
        if len(summaries) > _MAX_STAGE_BATCH_EVENTS:
            raise ValueError(f"at most {_MAX_STAGE_BATCH_EVENTS} stage summaries may be written at once")

        events_by_task: dict[str, list[StageProgressEvent]] = {}
        for event in events:
            events_by_task.setdefault(event.task_id, []).append(event)
        summaries_by_task: dict[str, StageProgressSummary] = {}
        for summary in summaries:
            previous = summaries_by_task.get(summary.task_id)
            if previous is None or _summary_order(summary) > _summary_order(previous):
                summaries_by_task[summary.task_id] = summary

        task_ids = sorted(set(events_by_task) | set(summaries_by_task))
        if not task_ids:
            return StageProgressWriteResult()

        coverage: list[StageProgressCoverage] = []
        missing_parent_task_ids: list[str] = []
        conflicted_task_ids: set[str] = set()
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                for task_id in task_ids:
                    task_conflicted_attempts: set[int] = set()
                    try:
                        # Serialize all writers for one task before reading its
                        # event coverage. The version marker below only updates
                        # once, so it cannot serve as a lock for later batches.
                        await connection.fetchrow("SELECT id FROM jobs WHERE id = $1 FOR UPDATE", task_id)
                        await connection.execute(
                            "UPDATE jobs SET stage_tracking_version = 1 WHERE id = $1 AND stage_tracking_version IS DISTINCT FROM 1",
                            task_id,
                        )
                        async with connection.transaction():
                            for event in sorted(
                                events_by_task.get(task_id, []), key=lambda item: (item.attempt, item.sequence)
                            ):
                                if await _insert_stage_event(connection, event):
                                    task_conflicted_attempts.add(event.attempt)

                            summary = summaries_by_task.get(task_id)
                            if summary is not None:
                                through = await _persisted_through_sequence(connection, task_id, summary.attempt)
                                await _upsert_stage_summary(
                                    connection,
                                    summary,
                                    persisted_through_sequence=through,
                                )

                            # Validate any complete invocation pairs after all
                            # out-of-order deliveries in this task group arrive.
                            event_attempts = {event.attempt for event in events_by_task.get(task_id, [])}
                            event_attempts.update(task_conflicted_attempts)
                            if summary is not None:
                                event_attempts.add(summary.attempt)
                            for attempt in event_attempts:
                                if await _has_conflicting_invocation_pair(connection, task_id, attempt):
                                    task_conflicted_attempts.add(attempt)
                                through = await _persisted_through_sequence(connection, task_id, attempt)
                                updated = await connection.fetchrow(
                                    """
                                    UPDATE job_stage_state
                                    SET persisted_through_sequence = GREATEST(persisted_through_sequence, $3),
                                        known_conflict = known_conflict OR $4,
                                        updated_at = now()
                                    WHERE task_id = $1 AND attempt = $2
                                    RETURNING task_id
                                    """,
                                    task_id,
                                    attempt,
                                    through,
                                    attempt in task_conflicted_attempts,
                                )
                                if updated is None and attempt in task_conflicted_attempts:
                                    await _upsert_stage_conflict_marker(
                                        connection,
                                        task_id,
                                        attempt,
                                        persisted_through_sequence=through,
                                    )
                                coverage.append(
                                    StageProgressCoverage(
                                        task_id=task_id,
                                        attempt=attempt,
                                        persisted_through_sequence=through,
                                    )
                                )
                    except asyncpg.ForeignKeyViolationError:
                        # A missing parent may be an expired job or a job write
                        # that never succeeded. Never resurrect it here.
                        missing_parent_task_ids.append(task_id)
                        coverage = [item for item in coverage if item.task_id != task_id]
                        continue
                    if task_conflicted_attempts:
                        conflicted_task_ids.add(task_id)

        return StageProgressWriteResult(
            coverage=coverage,
            missing_parent_task_ids=missing_parent_task_ids,
            conflicted_task_ids=sorted(conflicted_task_ids),
        )

    async def get_stage_progress_summaries(self, task_ids: list[str]) -> dict[str, DurableStageProgress]:
        if not task_ids:
            return {}
        rows = await self.pool.fetch(
            f"SELECT {_STAGE_STATE_COLUMNS} FROM job_stage_state WHERE task_id = ANY($1::text[]) AND has_summary",
            task_ids,
        )
        return {row["task_id"]: DurableStageProgress(**dict(row)) for row in rows}

    async def get_stage_progress_events(
        self,
        task_id: str,
        *,
        before: StageProgressEventCursor | None = None,
        limit: int = 100,
    ) -> StageProgressEventPage:
        if not 1 <= limit <= _MAX_STAGE_HISTORY_PAGE:
            raise ValueError(f"stage history page limit must be between 1 and {_MAX_STAGE_HISTORY_PAGE}")
        rows = await self.pool.fetch(
            f"""
            SELECT {_STAGE_EVENT_COLUMNS} FROM job_stage_events
            WHERE task_id = $1
              AND ($2::int IS NULL OR (attempt, sequence) < ($2, $3))
            ORDER BY attempt DESC, sequence DESC
            LIMIT $4
            """,
            task_id,
            before.attempt if before else None,
            before.sequence if before else None,
            limit + 1,
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        page_rows.reverse()
        events = [StageProgressEvent(**dict(row)) for row in page_rows]
        cursor = None
        if has_more and events:
            oldest = events[0]
            cursor = StageProgressEventCursor(attempt=oldest.attempt, sequence=oldest.sequence)
        return StageProgressEventPage(events=events, has_more=has_more, next_cursor=cursor)

    async def fail_orphaned_jobs(self, *, active_ids: list[str], error: str, before: datetime) -> int:
        return await self.pool.fetchval(
            """
            WITH failed AS (
                UPDATE jobs
                SET status = 'FAILED', error = $1, error_reason = $1, completed_at = now(), updated_at = now()
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


def _summary_order(summary: StageProgressSummary) -> tuple[int, int, int]:
    return summary.attempt, summary.latest_sequence, summary.snapshot_revision


def _same_event(row: asyncpg.Record, event: StageProgressEvent) -> bool:
    return (
        row["event_id"] == event.event_id
        and row["task_id"] == event.task_id
        and row["attempt"] == event.attempt
        and row["sequence"] == event.sequence
        and row["invocation_id"] == event.invocation_id
        and row["stage_id"] == event.stage_id
        and row["stage_rank"] == event.stage_rank
        and row["status"] == event.status.value
        and row["occurred_at"] == event.occurred_at
        and row["duration_ms"] == event.duration_ms
        and row["failure_effect"] == (event.failure_effect.value if event.failure_effect else None)
    )


async def _insert_stage_event(connection: asyncpg.Connection, event: StageProgressEvent) -> bool:
    """Insert an event; return True only when the existing observation conflicts."""
    inserted = await connection.fetchrow(
        f"""
        INSERT INTO job_stage_events ({_STAGE_EVENT_COLUMNS})
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT DO NOTHING
        RETURNING event_id
        """,
        event.event_id,
        event.task_id,
        event.attempt,
        event.sequence,
        event.invocation_id,
        event.stage_id,
        event.stage_rank,
        event.status.value,
        event.occurred_at,
        event.duration_ms,
        event.failure_effect.value if event.failure_effect else None,
    )
    if inserted is not None:
        return False

    existing = await connection.fetch(
        f"""
        SELECT {_STAGE_EVENT_COLUMNS} FROM job_stage_events
        WHERE event_id = $1
           OR (task_id = $2 AND attempt = $3 AND sequence = $4)
           OR (
               task_id = $2 AND attempt = $3 AND invocation_id = $5
               AND ((status = 'started' AND $6 = 'started')
                    OR (status IN ('completed', 'failed', 'cancelled')
                        AND $6 IN ('completed', 'failed', 'cancelled')))
           )
        """,
        event.event_id,
        event.task_id,
        event.attempt,
        event.sequence,
        event.invocation_id,
        event.status.value,
    )
    return not any(_same_event(row, event) for row in existing)


async def _persisted_through_sequence(connection: asyncpg.Connection, task_id: str, attempt: int) -> int:
    rows = await connection.fetch(
        "SELECT sequence FROM job_stage_events WHERE task_id = $1 AND attempt = $2 ORDER BY sequence",
        task_id,
        attempt,
    )
    expected = 1
    for row in rows:
        sequence = row["sequence"]
        if sequence < expected:
            continue
        if sequence != expected:
            break
        expected += 1
    return expected - 1


async def _upsert_stage_conflict_marker(
    connection: asyncpg.Connection,
    task_id: str,
    attempt: int,
    *,
    persisted_through_sequence: int,
) -> None:
    """Retain a conflict until a summary for this attempt is available."""
    await connection.execute(
        """
        INSERT INTO job_stage_state (
            task_id, attempt, latest_sequence, snapshot_revision, tracking_version, phase,
            persisted_through_sequence, known_conflict, has_summary
        )
        VALUES ($1, $2, 0, 0, 1, 'preparing', $3, true, false)
        ON CONFLICT (task_id) DO UPDATE SET
            attempt = EXCLUDED.attempt,
            latest_sequence = EXCLUDED.latest_sequence,
            snapshot_revision = EXCLUDED.snapshot_revision,
            tracking_version = EXCLUDED.tracking_version,
            phase = EXCLUDED.phase,
            current_stage_id = NULL,
            current_stage_rank = NULL,
            position_score = 0,
            stage_started_at = NULL,
            current_invocation_id = NULL,
            interrupted_invocation_id = NULL,
            persisted_through_sequence = EXCLUDED.persisted_through_sequence,
            expected_final_sequence = NULL,
            known_loss = false,
            known_conflict = true,
            has_open_invocation = false,
            history_truncated = false,
            has_summary = false,
            updated_at = now()
        WHERE EXCLUDED.attempt > job_stage_state.attempt
        """,
        task_id,
        attempt,
        persisted_through_sequence,
    )


async def _has_conflicting_invocation_pair(connection: asyncpg.Connection, task_id: str, attempt: int) -> bool:
    rows = await connection.fetch(
        """
        SELECT invocation_id, stage_id, sequence, status
        FROM job_stage_events
        WHERE task_id = $1 AND attempt = $2
        ORDER BY invocation_id, sequence
        """,
        task_id,
        attempt,
    )
    invocations: dict[object, dict[str, asyncpg.Record]] = {}
    for row in rows:
        pair = invocations.setdefault(row["invocation_id"], {})
        pair[row["status"]] = row
    for pair in invocations.values():
        started = pair.get(StageEventStatus.STARTED.value)
        ended = next(
            (
                pair.get(status.value)
                for status in (StageEventStatus.COMPLETED, StageEventStatus.FAILED, StageEventStatus.CANCELLED)
                if status.value in pair
            ),
            None,
        )
        if started is not None and ended is not None:
            if started["stage_id"] != ended["stage_id"] or ended["sequence"] <= started["sequence"]:
                return True
    return False


async def _upsert_stage_summary(
    connection: asyncpg.Connection,
    summary: StageProgressSummary,
    *,
    persisted_through_sequence: int,
) -> None:
    await connection.execute(
        """
        INSERT INTO job_stage_state (
            task_id, attempt, latest_sequence, snapshot_revision, tracking_version, phase,
            current_stage_id, current_stage_rank, position_score, stage_started_at,
            current_invocation_id, interrupted_invocation_id, persisted_through_sequence, expected_final_sequence,
            known_loss, known_conflict, has_open_invocation, history_truncated, has_summary, updated_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, false, $16, $17, true, now()
        )
        ON CONFLICT (task_id) DO UPDATE SET
            attempt = EXCLUDED.attempt,
            latest_sequence = EXCLUDED.latest_sequence,
            snapshot_revision = EXCLUDED.snapshot_revision,
            tracking_version = EXCLUDED.tracking_version,
            phase = EXCLUDED.phase,
            current_stage_id = EXCLUDED.current_stage_id,
            current_stage_rank = EXCLUDED.current_stage_rank,
            position_score = EXCLUDED.position_score,
            stage_started_at = EXCLUDED.stage_started_at,
            current_invocation_id = EXCLUDED.current_invocation_id,
            interrupted_invocation_id = EXCLUDED.interrupted_invocation_id,
            persisted_through_sequence = CASE
                WHEN EXCLUDED.attempt > job_stage_state.attempt THEN EXCLUDED.persisted_through_sequence
                ELSE GREATEST(job_stage_state.persisted_through_sequence, EXCLUDED.persisted_through_sequence)
            END,
            expected_final_sequence = CASE
                WHEN EXCLUDED.attempt > job_stage_state.attempt THEN EXCLUDED.expected_final_sequence
                WHEN job_stage_state.expected_final_sequence IS NOT NULL
                    AND job_stage_state.expected_final_sequence < EXCLUDED.latest_sequence
                    THEN EXCLUDED.expected_final_sequence
                ELSE COALESCE(job_stage_state.expected_final_sequence, EXCLUDED.expected_final_sequence)
            END,
            known_loss = CASE
                WHEN EXCLUDED.attempt > job_stage_state.attempt THEN EXCLUDED.known_loss
                ELSE job_stage_state.known_loss OR EXCLUDED.known_loss
            END,
            known_conflict = CASE
                WHEN EXCLUDED.attempt > job_stage_state.attempt THEN false
                ELSE job_stage_state.known_conflict
            END,
            has_open_invocation = EXCLUDED.has_open_invocation,
            has_summary = true,
            history_truncated = CASE
                WHEN EXCLUDED.attempt > job_stage_state.attempt THEN EXCLUDED.history_truncated
                ELSE job_stage_state.history_truncated OR EXCLUDED.history_truncated
            END,
            updated_at = now()
        WHERE EXCLUDED.attempt > job_stage_state.attempt
           OR (EXCLUDED.attempt = job_stage_state.attempt AND (
                NOT job_stage_state.has_summary
                OR EXCLUDED.latest_sequence > job_stage_state.latest_sequence
                OR (EXCLUDED.latest_sequence = job_stage_state.latest_sequence
                    AND EXCLUDED.snapshot_revision > job_stage_state.snapshot_revision)
           ))
        """,
        summary.task_id,
        summary.attempt,
        summary.latest_sequence,
        summary.snapshot_revision,
        summary.tracking_version,
        summary.phase.value,
        summary.current_stage_id,
        summary.current_stage_rank,
        summary.position_score,
        summary.stage_started_at,
        summary.current_invocation_id,
        summary.interrupted_invocation_id,
        persisted_through_sequence,
        summary.expected_final_sequence,
        summary.known_loss,
        summary.has_open_invocation,
        summary.history_truncated,
    )


__all__ = ["PgJobRepository"]
