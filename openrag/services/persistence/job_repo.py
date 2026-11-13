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

import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
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
from core.observability.metric_specs import STAGE_VALUES
from core.ports.job_repo import JobListSnapshot, JobRepository

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
    "current_substage_id, current_substage_label, current_substage_progress, current_substage_total, "
    "interrupted_invocation_id, persisted_through_sequence, expected_final_sequence, known_loss, known_conflict, "
    "has_open_invocation, history_truncated"
)


class _PgJobListSnapshot:
    def __init__(self, repository: PgJobRepository, connection: asyncpg.Connection) -> None:
        self._repository = repository
        self._connection = connection

    async def get_job_list_metadata_with_actor_rows(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        actor_task_ids: list[str],
    ) -> tuple[int, dict[str, int], dict[str, tuple[str, str | None]]]:
        return await self._repository.get_job_list_metadata_with_actor_rows(
            statuses=statuses,
            user_id=user_id,
            actor_task_ids=actor_task_ids,
            _connection=self._connection,
        )

    async def list_task_page_by_stage_order(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        actor_overlays: list[tuple[str, str | None, datetime | None]],
        offset: int = 0,
        limit: int = 50,
    ) -> list[tuple[str, IndexationJob | None]]:
        return await self._repository.list_task_page_by_stage_order(
            statuses=statuses,
            user_id=user_id,
            actor_overlays=actor_overlays,
            offset=offset,
            limit=limit,
            _connection=self._connection,
        )


class PgJobRepository(JobRepository):
    """Store one row per indexing task, keyed by task id."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    @asynccontextmanager
    async def task_list_snapshot(self) -> AsyncIterator[JobListSnapshot]:
        """Keep task-list metadata and page reads on one repeatable-read snapshot."""
        async with self.pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read"):
                yield _PgJobListSnapshot(self, connection)

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

    async def list_jobs_by_stage_order(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        exclude_task_ids: list[str] | None = None,
        offset: int = 0,
        limit: int = 50,
        _connection: asyncpg.Connection | None = None,
    ) -> list[IndexationJob]:
        """Page durable tasks in the same order used by the jobs UI."""
        connection = _connection if _connection is not None else self.pool
        rows = await connection.fetch(
            """
            SELECT jobs.* FROM jobs
            LEFT JOIN job_stage_state AS stage_progress
              ON stage_progress.task_id = jobs.id AND stage_progress.has_summary
            WHERE ($1::text[] IS NULL OR jobs.status = ANY($1::text[]))
              AND ($2::int IS NULL OR jobs.user_id = $2)
              AND NOT EXISTS (
                    SELECT 1
                    FROM unnest($3::text[]) AS excluded_task_ids(task_id)
                    WHERE excluded_task_ids.task_id = jobs.id
              )
            ORDER BY
                CASE
                    WHEN jobs.status = 'SERIALIZING' AND (
                        stage_progress.current_stage_id = ANY($6::text[])
                        OR (
                            stage_progress.phase = ANY($7::text[])
                            AND stage_progress.position_score > 0
                        )
                    ) THEN 0
                    WHEN jobs.status = 'SERIALIZING' THEN 1
                    WHEN jobs.status = 'QUEUED' THEN 2
                    WHEN jobs.status = ANY($8::text[]) THEN 3
                    ELSE 4
                END,
                CASE
                    WHEN jobs.status = 'SERIALIZING' AND (
                        stage_progress.current_stage_id = ANY($6::text[])
                        OR (
                            stage_progress.phase = ANY($7::text[])
                            AND stage_progress.position_score > 0
                        )
                    ) THEN stage_progress.position_score
                    ELSE 0
                END DESC,
                CASE
                    WHEN jobs.status = ANY($8::text[]) THEN -EXTRACT(EPOCH FROM jobs.created_at)
                    ELSE EXTRACT(EPOCH FROM jobs.created_at)
                END ASC,
                jobs.id ASC
            OFFSET $4 LIMIT $5
            """,
            statuses,
            user_id,
            exclude_task_ids or [],
            max(0, offset),
            max(1, limit),
            list(STAGE_VALUES),
            ["between_stages", "finalizing"],
            _TERMINAL_STATUSES,
        )
        return [self._row_to_job(row) for row in rows]

    async def list_task_page_by_stage_order(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        actor_overlays: list[tuple[str, str | None, datetime | None]],
        offset: int = 0,
        limit: int = 50,
        _connection: asyncpg.Connection | None = None,
    ) -> list[tuple[str, IndexationJob | None]]:
        """Page durable and actor-overlay rows together without widening the result window."""
        if not actor_overlays:
            jobs = await self.list_jobs_by_stage_order(
                statuses=statuses,
                user_id=user_id,
                exclude_task_ids=[],
                offset=offset,
                limit=limit,
                _connection=_connection,
            )
            return [(job.id, job) for job in jobs]

        actor_task_ids = [task_id for task_id, _, _ in actor_overlays]
        actor_states = [state for _, state, _ in actor_overlays]
        actor_created_at = [created_at for _, _, created_at in actor_overlays]
        connection = _connection if _connection is not None else self.pool
        rows = await connection.fetch(
            """
            WITH actor_overlays AS (
                SELECT * FROM unnest($3::text[], $4::text[], $5::timestamptz[])
                    AS actor(task_id, status, created_at)
            ),
            durable_candidates AS (
                SELECT jobs.id AS task_id, jobs.status, jobs.created_at,
                       stage_progress.current_stage_id, stage_progress.phase, stage_progress.position_score
                FROM jobs
                LEFT JOIN job_stage_state AS stage_progress
                  ON stage_progress.task_id = jobs.id AND stage_progress.has_summary
                WHERE ($1::text[] IS NULL OR jobs.status = ANY($1::text[]))
                  AND ($2::int IS NULL OR jobs.user_id = $2)
                  AND NOT EXISTS (
                        SELECT 1 FROM actor_overlays AS actor WHERE actor.task_id = jobs.id
                  )
            ),
            actor_candidates AS (
                SELECT actor.task_id, actor.status,
                       COALESCE(jobs.created_at, actor.created_at) AS created_at,
                       stage_progress.current_stage_id, stage_progress.phase, stage_progress.position_score
                FROM actor_overlays AS actor
                LEFT JOIN jobs ON jobs.id = actor.task_id
                LEFT JOIN job_stage_state AS stage_progress
                  ON stage_progress.task_id = actor.task_id AND stage_progress.has_summary
                WHERE ($1::text[] IS NULL OR actor.status = ANY($1::text[]))
            ),
            candidates AS (
                SELECT * FROM durable_candidates
                UNION ALL
                SELECT * FROM actor_candidates
            ),
            page AS MATERIALIZED (
                SELECT * FROM candidates
                ORDER BY
                    CASE
                        WHEN status = 'SERIALIZING' AND (
                            current_stage_id = ANY($8::text[])
                            OR (phase = ANY($9::text[]) AND position_score > 0)
                        ) THEN 0
                        WHEN status = 'SERIALIZING' THEN 1
                        WHEN status = 'QUEUED' THEN 2
                        WHEN status = ANY($10::text[]) THEN 3
                        ELSE 4
                    END,
                    CASE
                        WHEN status = 'SERIALIZING' AND (
                            current_stage_id = ANY($8::text[])
                            OR (phase = ANY($9::text[]) AND position_score > 0)
                        ) THEN position_score
                        ELSE 0
                    END DESC,
                    CASE
                        WHEN status = ANY($10::text[]) THEN -EXTRACT(EPOCH FROM created_at)
                        ELSE EXTRACT(EPOCH FROM created_at)
                    END ASC,
                    task_id ASC
                OFFSET $6 LIMIT $7
            )
            SELECT page.task_id,
                   CASE WHEN jobs.id IS NULL THEN NULL::text ELSE to_jsonb(jobs)::text END AS job_json
            FROM page
            LEFT JOIN jobs ON jobs.id = page.task_id
            ORDER BY
                CASE
                    WHEN page.status = 'SERIALIZING' AND (
                        page.current_stage_id = ANY($8::text[])
                        OR (page.phase = ANY($9::text[]) AND page.position_score > 0)
                    ) THEN 0
                    WHEN page.status = 'SERIALIZING' THEN 1
                    WHEN page.status = 'QUEUED' THEN 2
                    WHEN page.status = ANY($10::text[]) THEN 3
                    ELSE 4
                END,
                CASE
                    WHEN page.status = 'SERIALIZING' AND (
                        page.current_stage_id = ANY($8::text[])
                        OR (page.phase = ANY($9::text[]) AND page.position_score > 0)
                    ) THEN page.position_score
                    ELSE 0
                END DESC,
                CASE
                    WHEN page.status = ANY($10::text[]) THEN -EXTRACT(EPOCH FROM page.created_at)
                    ELSE EXTRACT(EPOCH FROM page.created_at)
                END ASC,
                page.task_id ASC
            """,
            statuses,
            user_id,
            actor_task_ids,
            actor_states,
            actor_created_at,
            max(0, offset),
            max(1, limit),
            list(STAGE_VALUES),
            ["between_stages", "finalizing"],
            _TERMINAL_STATUSES,
        )
        page: list[tuple[str, IndexationJob | None]] = []
        for row in rows:
            job_json = row["job_json"]
            job = self._row_to_job(json.loads(job_json)) if job_json is not None else None
            page.append((row["task_id"], job))
        return page

    async def get_job_list_metadata(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
    ) -> tuple[int, dict[str, int]]:
        rows = await self.pool.fetch(
            """
            SELECT partition, COUNT(*)::int AS count FROM jobs
            WHERE ($1::text[] IS NULL OR status = ANY($1::text[]))
              AND ($2::int IS NULL OR user_id = $2)
            GROUP BY partition
            """,
            statuses,
            user_id,
        )
        partition_counts = {row["partition"]: int(row["count"]) for row in rows}
        return sum(partition_counts.values()), partition_counts

    async def get_job_list_metadata_with_actor_rows(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        actor_task_ids: list[str],
        _connection: asyncpg.Connection | None = None,
    ) -> tuple[int, dict[str, int], dict[str, tuple[str, str | None]]]:
        """Read counts and actor-held durable states from one PostgreSQL snapshot."""
        connection = _connection if _connection is not None else self.pool
        rows = await connection.fetch(
            """
            WITH matching_partitions AS (
                SELECT partition, COUNT(*)::int AS count
                FROM jobs
                WHERE ($1::int IS NULL OR user_id = $1)
                  AND ($2::text[] IS NULL OR status = ANY($2::text[]))
                GROUP BY partition
            )
            SELECT 'partition'::text AS row_type, partition, count, NULL::text AS id, NULL::text AS status
            FROM matching_partitions
            UNION ALL
            SELECT 'actor'::text AS row_type, partition, 0::int AS count, id, status
            FROM jobs
            WHERE ($1::int IS NULL OR user_id = $1)
              AND id = ANY($3::text[])
            """,
            user_id,
            statuses,
            actor_task_ids,
        )
        partition_counts: dict[str, int] = {}
        actor_rows: dict[str, tuple[str, str | None]] = {}
        for row in rows:
            if row["row_type"] == "partition":
                partition_counts[row["partition"]] = int(row["count"])
            else:
                actor_rows[row["id"]] = (row["status"], row["partition"])
        return sum(partition_counts.values()), partition_counts, actor_rows

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
        permanent_failed_task_ids: list[str] = []
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
                    except asyncpg.ForeignKeyViolationError:
                        # A missing parent may be an expired job or a job write
                        # that never succeeded. Never resurrect it here.
                        missing_parent_task_ids.append(task_id)
                        coverage = [item for item in coverage if item.task_id != task_id]
                        continue
                    try:
                        # Keep each task isolated by its own savepoint. A bad
                        # event must not roll back valid tasks in this batch.
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
                                        interrupted_invocation_id = CASE
                                            WHEN EXISTS (
                                                SELECT 1
                                                FROM job_stage_events AS event
                                                WHERE event.task_id = job_stage_state.task_id
                                                  AND event.attempt = job_stage_state.attempt
                                                  AND event.invocation_id = job_stage_state.interrupted_invocation_id
                                                  AND event.status IN ('completed', 'failed', 'cancelled')
                                            ) THEN NULL
                                            ELSE job_stage_state.interrupted_invocation_id
                                        END,
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
                        # The savepoint rolls back this task only; continue the
                        # outer transaction so unrelated task writes commit.
                        missing_parent_task_ids.append(task_id)
                        coverage = [item for item in coverage if item.task_id != task_id]
                        continue
                    except (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError):
                        # Data and constraint failures cannot recover on retry.
                        # The failed task is reported to the writer, while its
                        # sibling tasks remain eligible to commit.
                        permanent_failed_task_ids.append(task_id)
                        coverage = [item for item in coverage if item.task_id != task_id]
                        continue
                    if task_conflicted_attempts:
                        conflicted_task_ids.add(task_id)

        return StageProgressWriteResult(
            coverage=coverage,
            missing_parent_task_ids=missing_parent_task_ids,
            conflicted_task_ids=sorted(conflicted_task_ids),
            permanent_failed_task_ids=permanent_failed_task_ids,
        )

    async def get_stage_progress_summaries(self, task_ids: list[str]) -> dict[str, DurableStageProgress]:
        if not task_ids:
            return {}
        rows = await self.pool.fetch(
            f"SELECT {_STAGE_STATE_COLUMNS} FROM job_stage_state "
            "WHERE task_id = ANY($1::text[]) AND (has_summary OR known_conflict)",
            task_ids,
        )
        return {row["task_id"]: DurableStageProgress(**dict(row)) for row in rows}

    async def mark_stage_progress_interrupted(self, task_id: str) -> bool:
        """Persist worker settlement, then mark its open stage if one is present."""
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                settled = await connection.fetchrow(
                    """
                    UPDATE jobs
                    SET stage_interruption_confirmed_attempt = GREATEST(
                        COALESCE((SELECT attempt FROM job_stage_state WHERE task_id = jobs.id), 0),
                        COALESCE((SELECT MAX(attempt) FROM job_stage_events WHERE task_id = jobs.id), 0),
                        1
                    )
                    WHERE id = $1 AND status IN ('FAILED', 'CANCELLED')
                    RETURNING stage_interruption_confirmed_attempt
                    """,
                    task_id,
                )
                if settled is None:
                    return False

                marked = await connection.fetchrow(
                    """
                    UPDATE job_stage_state AS stage
                    SET interrupted_invocation_id = stage.current_invocation_id,
                        updated_at = now()
                    WHERE stage.task_id = $1
                      AND stage.attempt = $2
                      AND stage.has_summary
                      AND stage.has_open_invocation
                      AND stage.current_invocation_id IS NOT NULL
                      AND stage.interrupted_invocation_id IS DISTINCT FROM stage.current_invocation_id
                      AND NOT EXISTS (
                          SELECT 1
                          FROM job_stage_events AS event
                          WHERE event.task_id = stage.task_id
                            AND event.attempt = stage.attempt
                            AND event.invocation_id = stage.current_invocation_id
                            AND event.status IN ('completed', 'failed', 'cancelled')
                      )
                    RETURNING stage.current_invocation_id
                    """,
                    task_id,
                    settled["stage_interruption_confirmed_attempt"],
                )
                return marked is not None

    async def get_latest_stage_ids(self, task_ids: list[str]) -> dict[str, str]:
        if not task_ids:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT ON (task_id) task_id, stage_id
            FROM job_stage_events
            WHERE task_id = ANY($1::text[])
            ORDER BY task_id, attempt DESC, sequence DESC
            """,
            task_ids,
        )
        return {row["task_id"]: row["stage_id"] for row in rows}

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
            current_substage_id = NULL,
            current_substage_label = NULL,
            current_substage_progress = NULL,
            current_substage_total = NULL,
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
            known_loss, known_conflict, has_open_invocation, history_truncated,
            current_substage_id, current_substage_label, current_substage_progress, current_substage_total,
            has_summary, updated_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
            CASE
                WHEN $16 AND $11::uuid IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM jobs
                        WHERE jobs.id = $1::varchar
                          AND jobs.status IN ('FAILED', 'CANCELLED')
                          AND jobs.stage_interruption_confirmed_attempt = $2
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM job_stage_events AS event
                        WHERE event.task_id = $1::varchar
                          AND event.attempt = $2
                          AND event.invocation_id = $11
                          AND event.status IN ('completed', 'failed', 'cancelled')
                    )
                    THEN $11::uuid
                ELSE $12::uuid
            END,
            $13, $14, $15, false, $16, $17, $18, $19, $20, $21, true, now()
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
            interrupted_invocation_id = CASE
                WHEN EXCLUDED.has_open_invocation
                    AND EXCLUDED.current_invocation_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM jobs
                        WHERE jobs.id = EXCLUDED.task_id
                          AND jobs.status IN ('FAILED', 'CANCELLED')
                          AND jobs.stage_interruption_confirmed_attempt = EXCLUDED.attempt
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM job_stage_events AS event
                        WHERE event.task_id = EXCLUDED.task_id
                          AND event.attempt = EXCLUDED.attempt
                          AND event.invocation_id = EXCLUDED.current_invocation_id
                          AND event.status IN ('completed', 'failed', 'cancelled')
                    )
                    THEN EXCLUDED.current_invocation_id
                WHEN EXCLUDED.attempt = job_stage_state.attempt
                    AND EXCLUDED.has_open_invocation
                    AND EXCLUDED.current_invocation_id = job_stage_state.interrupted_invocation_id
                    AND NOT EXISTS (
                        SELECT 1 FROM job_stage_events AS event
                        WHERE event.task_id = EXCLUDED.task_id
                          AND event.attempt = EXCLUDED.attempt
                          AND event.invocation_id = EXCLUDED.current_invocation_id
                          AND event.status IN ('completed', 'failed', 'cancelled')
                    )
                    THEN job_stage_state.interrupted_invocation_id
                ELSE EXCLUDED.interrupted_invocation_id
            END,
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
            current_substage_id = EXCLUDED.current_substage_id,
            current_substage_label = EXCLUDED.current_substage_label,
            current_substage_progress = EXCLUDED.current_substage_progress,
            current_substage_total = EXCLUDED.current_substage_total,
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
        summary.current_substage_id,
        summary.current_substage_label,
        summary.current_substage_progress,
        summary.current_substage_total,
    )
    if summary.known_loss:
        # A loss-only marker must survive even when its snapshot is no newer
        # than the durable row. Keep this update separate so stale marker
        # fields cannot overwrite a newer projection.
        await connection.execute(
            """
            UPDATE job_stage_state
            SET known_loss = true, updated_at = now()
            WHERE task_id = $1 AND attempt = $2 AND NOT known_loss
            """,
            summary.task_id,
            summary.attempt,
        )


__all__ = ["PgJobRepository"]
