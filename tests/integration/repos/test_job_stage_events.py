"""Per-task stage-event writes against real PostgreSQL constraints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from core.models.catalog import DocumentStatus, IndexationJob
from core.models.stage_progress import (
    StageEventStatus,
    StageFailureEffect,
    StageProgressEvent,
    StageProgressSummary,
    StageTaskPhase,
)
from services.persistence.connection import _ALEMBIC_INI, _MIGRATIONS_DIR
from sqlalchemy import URL

from .conftest import _admin_dsn, _admin_dsn_parts, _connect_admin

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_PREVIOUS_HEAD = "5e6d7c8b9a01"
_STAGE_HISTORY_REVISION = "bcbdf503a6f1"


def _alembic_config(database: str) -> Config:
    parts = _admin_dsn_parts()
    url = URL.create(
        "postgresql",
        username=str(parts["user"]),
        password=str(parts["password"]),
        host=str(parts["host"]),
        port=int(parts["port"]),
        database=database,
    ).render_as_string(hide_password=False)
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest_asyncio.fixture(loop_scope="session")
async def legacy_jobs_db():
    admin = await _connect_admin()
    if admin is None:
        pytest.skip("Postgres unreachable; set POSTGRES_TEST_ADMIN_DSN or start the rdb container.")
    database = f"openrag_stage_migration_{uuid4().hex[:8]}"
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()

    cfg = _alembic_config(database)
    await asyncio.to_thread(command.upgrade, cfg, _PREVIOUS_HEAD)
    dsn = _admin_dsn().rsplit("/", 1)[0] + f"/{database}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            "INSERT INTO jobs (id, partition, status) VALUES ('legacy-complete', 'p', 'COMPLETED'), ('legacy-active', 'p', 'SERIALIZING')"
        )
        yield connection, cfg
    finally:
        await connection.close()
        admin = await _connect_admin()
        if admin is not None:
            try:
                await admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
                    database,
                )
                await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
            finally:
                await admin.close()


def _events(
    task_id: str,
    *,
    attempt: int = 1,
    terminal_status: StageEventStatus = StageEventStatus.COMPLETED,
):
    invocation_id = uuid4()
    return [
        StageProgressEvent(
            task_id=task_id,
            attempt=attempt,
            sequence=1,
            invocation_id=invocation_id,
            stage_id="parse",
            stage_rank=0,
            status=StageEventStatus.STARTED,
            occurred_at=datetime.now(UTC),
        ),
        StageProgressEvent(
            task_id=task_id,
            attempt=attempt,
            sequence=2,
            invocation_id=invocation_id,
            stage_id="parse",
            stage_rank=0,
            status=terminal_status,
            occurred_at=datetime.now(UTC),
            duration_ms=125,
            failure_effect=StageFailureEffect.TASK_FAILED if terminal_status is StageEventStatus.FAILED else None,
        ),
    ]


def _summary(task_id: str, *, attempt: int = 1, final_sequence: int = 2, revision: int = 3) -> StageProgressSummary:
    return StageProgressSummary(
        task_id=task_id,
        attempt=attempt,
        latest_sequence=final_sequence,
        snapshot_revision=revision,
        phase=StageTaskPhase.BETWEEN_STAGES,
        current_stage_rank=0,
        position_score=0,
        expected_final_sequence=final_sequence,
    )


async def test_stage_event_delivery_is_idempotent_and_reports_conflicting_outcomes(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))

    events = _events(task_id)
    first = await repo.write_stage_progress(events=events, summaries=[_summary(task_id)])
    duplicate = await repo.write_stage_progress(events=events, summaries=[_summary(task_id)])

    assert first.coverage[0].persisted_through_sequence == 2
    assert duplicate.conflicted_task_ids == []
    assert await postgres_store.pool.fetchval("SELECT stage_tracking_version FROM jobs WHERE id = $1", task_id) == 1
    page = await repo.get_stage_progress_events(task_id, limit=100)
    assert [event.sequence for event in page.events] == [1, 2]
    assert page.has_more is False

    conflicting_terminal = events[1].model_copy(
        update={
            "event_id": uuid4(),
            "sequence": 3,
            "status": StageEventStatus.FAILED,
            "failure_effect": StageFailureEffect.TASK_FAILED,
        }
    )
    conflict = await repo.write_stage_progress(
        events=[conflicting_terminal],
        summaries=[_summary(task_id, final_sequence=3, revision=4)],
    )

    assert conflict.conflicted_task_ids == [task_id]
    state = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert state.persisted_through_sequence == 2
    assert state.latest_sequence == 3
    assert state.expected_final_sequence == 3
    assert state.known_conflict is True
    assert [event.sequence for event in (await repo.get_stage_progress_events(task_id, limit=100)).events] == [1, 2]

    unknown_final = StageProgressSummary(
        task_id=task_id,
        latest_sequence=4,
        snapshot_revision=5,
        phase=StageTaskPhase.BETWEEN_STAGES,
        current_stage_rank=0,
        position_score=0,
    )
    await repo.write_stage_progress(events=[], summaries=[unknown_final])
    state_with_unknown_final = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert state_with_unknown_final.latest_sequence == 4
    assert state_with_unknown_final.expected_final_sequence is None

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_conflict_before_first_summary_is_retained_until_summary_arrives(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-{uuid4()}"
    previous_attempt_task_id = f"stage-history-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))
    await repo.upsert_job(
        IndexationJob(id=previous_attempt_task_id, status=DocumentStatus.SERIALIZING, partition="test")
    )

    started = _events(task_id)[0]
    await repo.write_stage_progress(events=[started], summaries=[])
    conflicting_start = started.model_copy(update={"event_id": uuid4(), "invocation_id": uuid4()})
    result = await repo.write_stage_progress(events=[conflicting_start], summaries=[])

    assert result.conflicted_task_ids == [task_id]
    assert await repo.get_stage_progress_summaries([task_id]) == {}
    marker = await postgres_store.pool.fetchrow(
        "SELECT attempt, known_conflict, has_summary FROM job_stage_state WHERE task_id = $1",
        task_id,
    )
    assert (marker["attempt"], marker["known_conflict"], marker["has_summary"]) == (1, True, False)

    await repo.write_stage_progress(events=[], summaries=[_summary(task_id, final_sequence=1, revision=1)])
    summary = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert summary.latest_sequence == 1
    assert summary.known_conflict is True

    await repo.write_stage_progress(
        events=[],
        summaries=[_summary(previous_attempt_task_id, attempt=1, final_sequence=0, revision=0)],
    )
    previous_attempt_start = _events(previous_attempt_task_id, attempt=2)[0]
    await repo.write_stage_progress(events=[previous_attempt_start], summaries=[])
    previous_attempt_conflict = previous_attempt_start.model_copy(
        update={"event_id": uuid4(), "invocation_id": uuid4()}
    )
    result = await repo.write_stage_progress(events=[previous_attempt_conflict], summaries=[])

    assert result.conflicted_task_ids == [previous_attempt_task_id]
    assert await repo.get_stage_progress_summaries([previous_attempt_task_id]) == {}
    marker = await postgres_store.pool.fetchrow(
        "SELECT attempt, known_conflict, has_summary FROM job_stage_state WHERE task_id = $1",
        previous_attempt_task_id,
    )
    assert (marker["attempt"], marker["known_conflict"], marker["has_summary"]) == (2, True, False)

    await repo.write_stage_progress(
        events=[],
        summaries=[_summary(previous_attempt_task_id, attempt=2, final_sequence=1, revision=1)],
    )
    previous_attempt_summary = (await repo.get_stage_progress_summaries([previous_attempt_task_id]))[
        previous_attempt_task_id
    ]
    assert previous_attempt_summary.latest_sequence == 1
    assert previous_attempt_summary.known_conflict is True

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)
    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", previous_attempt_task_id)


async def test_stage_summary_persists_interrupted_invocation_and_resets_for_retry(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))

    invocation_id = uuid4()
    started_at = datetime.now(UTC)
    started = StageProgressEvent(
        task_id=task_id,
        attempt=1,
        sequence=1,
        invocation_id=invocation_id,
        stage_id="parse",
        stage_rank=0,
        status=StageEventStatus.STARTED,
        occurred_at=started_at,
    )
    interrupted = StageProgressSummary(
        task_id=task_id,
        attempt=1,
        latest_sequence=1,
        snapshot_revision=1,
        phase=StageTaskPhase.STAGE,
        current_stage_id="parse",
        current_stage_rank=0,
        stage_started_at=started_at,
        current_invocation_id=invocation_id,
        interrupted_invocation_id=invocation_id,
        has_open_invocation=True,
    )
    await repo.write_stage_progress(events=[started], summaries=[interrupted])

    persisted = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert persisted.interrupted_invocation_id == invocation_id

    await repo.write_stage_progress(
        events=[],
        summaries=[StageProgressSummary(task_id=task_id, attempt=2, phase=StageTaskPhase.PREPARING)],
    )
    retried = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert retried.attempt == 2
    assert retried.interrupted_invocation_id is None

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_out_of_order_events_advance_only_contiguous_coverage(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))
    started, completed = _events(task_id)

    gap = await repo.write_stage_progress(events=[completed], summaries=[_summary(task_id)])
    assert gap.coverage[0].persisted_through_sequence == 0
    assert (await repo.get_stage_progress_summaries([task_id]))[task_id].persisted_through_sequence == 0

    repaired = await repo.write_stage_progress(
        events=[started],
        summaries=[_summary(task_id, revision=4)],
    )
    state = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert repaired.coverage[0].persisted_through_sequence == 2
    assert state.persisted_through_sequence == 2
    assert state.known_conflict is False

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_concurrent_writes_do_not_leave_persisted_coverage_behind(postgres_store) -> None:
    """Writers for one task serialize coverage updates on the parent job row."""
    repo = postgres_store.job_repo
    task_id = f"stage-lock-{uuid4()}"
    trigger_function = f"slow_stage_state_update_{uuid4().hex}"
    trigger_name = f"slow_stage_state_update_{uuid4().hex}"
    function_created = False
    trigger_created = False
    try:
        await repo.upsert_job(
            IndexationJob(
                id=task_id,
                status=DocumentStatus.SERIALIZING,
                partition="test",
                stage_tracking_version=1,
            )
        )
        await repo.write_stage_progress(
            events=[],
            summaries=[
                StageProgressSummary(
                    task_id=task_id,
                    attempt=1,
                    latest_sequence=0,
                    snapshot_revision=0,
                    phase=StageTaskPhase.PREPARING,
                )
            ],
        )
        await postgres_store.pool.execute(
            f"""
            CREATE FUNCTION {trigger_function}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.task_id = '{task_id}' AND NEW.persisted_through_sequence = 1 THEN
                    PERFORM pg_sleep(0.35);
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        function_created = True
        await postgres_store.pool.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE ON job_stage_state
            FOR EACH ROW EXECUTE FUNCTION {trigger_function}()
            """
        )
        trigger_created = True

        invocation_id = uuid4()
        started = StageProgressEvent(
            task_id=task_id,
            attempt=1,
            sequence=1,
            invocation_id=invocation_id,
            stage_id="parse",
            stage_rank=0,
            status=StageEventStatus.STARTED,
            occurred_at=datetime.now(UTC),
        )
        completed = started.model_copy(
            update={
                "event_id": uuid4(),
                "sequence": 2,
                "status": StageEventStatus.COMPLETED,
                "occurred_at": datetime.now(UTC),
                "duration_ms": 350,
            }
        )
        started_summary = StageProgressSummary(
            task_id=task_id,
            attempt=1,
            latest_sequence=1,
            snapshot_revision=1,
            phase=StageTaskPhase.STAGE,
            current_stage_id="parse",
            current_stage_rank=0,
            current_invocation_id=invocation_id,
            has_open_invocation=True,
        )
        first_write = asyncio.create_task(repo.write_stage_progress(events=[started], summaries=[started_summary]))
        await asyncio.sleep(0.05)
        await repo.write_stage_progress(events=[completed], summaries=[])
        await first_write

        state = (await repo.get_stage_progress_summaries([task_id]))[task_id]
        assert state.persisted_through_sequence == 2
    finally:
        if trigger_created:
            await postgres_store.pool.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON job_stage_state")
        if function_created:
            await postgres_store.pool.execute(f"DROP FUNCTION IF EXISTS {trigger_function}()")
        await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_late_previous_attempt_is_retained_without_changing_current_summary(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))
    first_attempt = _events(task_id, attempt=1)
    second_attempt = _events(task_id, attempt=2)
    await repo.write_stage_progress(events=second_attempt, summaries=[_summary(task_id, attempt=2)])

    await repo.write_stage_progress(events=first_attempt, summaries=[_summary(task_id, attempt=1)])

    state = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    page = await repo.get_stage_progress_events(task_id, limit=100)
    assert state.attempt == 2
    assert state.latest_sequence == 2
    assert [(event.attempt, event.sequence) for event in page.events] == [(1, 1), (1, 2), (2, 1), (2, 2)]

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_event_history_pages_are_bounded_and_stay_in_execution_order(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))
    await repo.write_stage_progress(
        events=_events(task_id, attempt=1) + _events(task_id, attempt=2),
        summaries=[_summary(task_id, attempt=1), _summary(task_id, attempt=2)],
    )

    newest = await repo.get_stage_progress_events(task_id, limit=2)
    oldest = await repo.get_stage_progress_events(task_id, before=newest.next_cursor, limit=2)

    assert [(event.attempt, event.sequence) for event in newest.events] == [(2, 1), (2, 2)]
    assert newest.has_more is True
    assert newest.next_cursor is not None
    assert [(event.attempt, event.sequence) for event in oldest.events] == [(1, 1), (1, 2)]
    assert oldest.has_more is False
    assert oldest.next_cursor is None

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_missing_parent_does_not_discard_another_tasks_events(postgres_store) -> None:
    repo = postgres_store.job_repo
    healthy_task_id = f"stage-history-{uuid4()}"
    missing_task_id = f"missing-stage-parent-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=healthy_task_id, status=DocumentStatus.SERIALIZING, partition="test"))

    result = await repo.write_stage_progress(
        events=_events(missing_task_id) + _events(healthy_task_id),
        summaries=[_summary(missing_task_id), _summary(healthy_task_id)],
    )

    assert result.missing_parent_task_ids == [missing_task_id]
    assert {row.task_id for row in result.coverage} == {healthy_task_id}
    assert len((await repo.get_stage_progress_events(healthy_task_id, limit=100)).events) == 2
    assert (await repo.get_stage_progress_events(missing_task_id, limit=100)).events == []


async def test_deleting_a_job_cascades_stage_history_and_projection(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))
    await repo.write_stage_progress(events=_events(task_id), summaries=[_summary(task_id)])

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)

    assert (await repo.get_stage_progress_events(task_id, limit=100)).events == []
    assert await repo.get_stage_progress_summaries([task_id]) == {}


async def test_migration_preserves_existing_jobs_without_inventing_history(legacy_jobs_db) -> None:
    connection, cfg = legacy_jobs_db

    await asyncio.to_thread(command.upgrade, cfg, _STAGE_HISTORY_REVISION)
    await asyncio.to_thread(command.upgrade, cfg, _STAGE_HISTORY_REVISION)

    jobs = await connection.fetch("SELECT id, status, stage_tracking_version FROM jobs ORDER BY id")
    assert [(row["id"], row["status"], row["stage_tracking_version"]) for row in jobs] == [
        ("legacy-active", "SERIALIZING", None),
        ("legacy-complete", "COMPLETED", 0),
    ]
    assert await connection.fetchval("SELECT COUNT(*) FROM job_stage_events") == 0
    assert await connection.fetchval("SELECT COUNT(*) FROM job_stage_state") == 0

    await asyncio.to_thread(command.downgrade, cfg, _PREVIOUS_HEAD)

    assert await connection.fetchval("SELECT COUNT(*) FROM jobs") == 2
    assert await connection.fetchval("SELECT to_regclass('job_stage_events')") is None
    assert await connection.fetchval("SELECT to_regclass('job_stage_state')") is None
    assert (
        await connection.fetchval(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'jobs' AND column_name = 'stage_tracking_version'"
        )
        == 0
    )
