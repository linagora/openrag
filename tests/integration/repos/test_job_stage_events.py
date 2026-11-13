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
from services.workers.stage_event_writer import StageEventWriter
from sqlalchemy import URL

from .conftest import _admin_dsn, _admin_dsn_parts, _connect_admin

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_PREVIOUS_HEAD = "5e6d7c8b9a01"
_STAGE_HISTORY_REVISION = "bcbdf503a6f1"
_STAGE_TRACKING_BACKFILL_REVISION = "c4d8f2a6b901"


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


async def test_list_jobs_by_stage_order_uses_persisted_position_scores(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_ids = {
        "lower": f"stage-order-{uuid4()}",
        "highest": f"stage-order-{uuid4()}",
        "middle": f"stage-order-{uuid4()}",
    }
    scores = {"lower": 120, "highest": 920, "middle": 510}
    for name, task_id in task_ids.items():
        await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="stage-order"))
        await repo.write_stage_progress(
            events=[],
            summaries=[
                StageProgressSummary(
                    task_id=task_id,
                    latest_sequence=0,
                    snapshot_revision=1,
                    phase=StageTaskPhase.FINALIZING,
                    position_score=scores[name],
                )
            ],
        )

    jobs = await repo.list_jobs_by_stage_order(
        statuses=[DocumentStatus.SERIALIZING.value],
        exclude_task_ids=[],
        offset=0,
        limit=len(task_ids),
    )

    assert [job.id for job in jobs] == [task_ids["highest"], task_ids["middle"], task_ids["lower"]]


async def test_list_jobs_by_stage_order_keeps_finished_tasks_newest_first(postgres_store) -> None:
    repo = postgres_store.job_repo
    older_id = f"finished-order-{uuid4()}"
    newer_id = f"finished-order-{uuid4()}"
    older = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    newer = older.replace(hour=11)
    for task_id, status, created_at in (
        (older_id, DocumentStatus.COMPLETED, older),
        (newer_id, DocumentStatus.FAILED, newer),
    ):
        await repo.upsert_job(IndexationJob(id=task_id, status=status, partition="finished-order"))
        await postgres_store.pool.execute("UPDATE jobs SET created_at = $1 WHERE id = $2", created_at, task_id)

    jobs = await repo.list_jobs_by_stage_order(
        statuses=[DocumentStatus.COMPLETED.value, DocumentStatus.FAILED.value],
        exclude_task_ids=[],
        offset=0,
        limit=2,
    )

    assert [job.id for job in jobs] == [newer_id, older_id]


async def test_list_task_page_by_stage_order_merges_actor_only_rows(postgres_store) -> None:
    repo = postgres_store.job_repo
    actor_id = f"actor-only-{uuid4()}"
    durable_id = f"durable-{uuid4()}"
    actor_created_at = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    await repo.upsert_job(IndexationJob(id=durable_id, status=DocumentStatus.COMPLETED, partition="stage-order"))
    await postgres_store.pool.execute("UPDATE jobs SET created_at = $1 WHERE id = $2", actor_created_at, durable_id)

    page = await repo.list_task_page_by_stage_order(
        actor_overlays=[(actor_id, DocumentStatus.QUEUED.value, actor_created_at)],
        offset=0,
        limit=2,
    )

    assert [(task_id, job.id if job is not None else None) for task_id, job in page] == [
        (actor_id, None),
        (durable_id, durable_id),
    ]

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", durable_id)


async def test_job_list_metadata_snapshot_includes_actor_rows_and_latest_stage(postgres_store) -> None:
    repo = postgres_store.job_repo
    actor_task_id = f"list-snapshot-{uuid4()}"
    completed_task_id = f"list-snapshot-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=actor_task_id, status=DocumentStatus.FAILED, partition="actor-p"))
    await repo.upsert_job(IndexationJob(id=completed_task_id, status=DocumentStatus.COMPLETED, partition="done-p"))
    await repo.write_stage_progress(
        events=_events(actor_task_id, terminal_status=StageEventStatus.FAILED),
        summaries=[_summary(actor_task_id)],
    )

    total, partition_counts, actor_rows = await repo.get_job_list_metadata_with_actor_rows(
        statuses=[DocumentStatus.COMPLETED.value],
        actor_task_ids=[actor_task_id],
    )
    latest_stage_ids = await repo.get_latest_stage_ids([actor_task_id])

    assert total == 1
    assert partition_counts == {"done-p": 1}
    assert actor_rows == {actor_task_id: (DocumentStatus.FAILED.value, "actor-p")}
    assert latest_stage_ids == {actor_task_id: "parse"}

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = ANY($1::text[])", [actor_task_id, completed_task_id])


async def test_task_list_snapshot_keeps_metadata_and_page_consistent(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"list-repeatable-read-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="snapshot-p"))

    try:
        async with repo.task_list_snapshot() as snapshot:
            total, _, _ = await snapshot.get_job_list_metadata_with_actor_rows(
                statuses=[DocumentStatus.SERIALIZING.value],
                actor_task_ids=[],
            )
            assert total == 1

            await postgres_store.pool.execute(
                "UPDATE jobs SET status = $1 WHERE id = $2",
                DocumentStatus.COMPLETED.value,
                task_id,
            )
            page = await snapshot.list_task_page_by_stage_order(
                statuses=[DocumentStatus.SERIALIZING.value],
                actor_overlays=[],
                offset=0,
                limit=1,
            )

        assert [job.id for _, job in page if job is not None] == [task_id]
        assert (await repo.get_job(task_id)).status is DocumentStatus.COMPLETED
    finally:
        await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


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

    stale_loss_marker = unknown_final.model_copy(
        update={"latest_sequence": 3, "snapshot_revision": 4, "known_loss": True}
    )
    await repo.write_stage_progress(events=[], summaries=[stale_loss_marker])
    state_with_known_loss = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert state_with_known_loss.latest_sequence == 4
    assert state_with_known_loss.snapshot_revision == 5
    assert state_with_known_loss.known_loss is True

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_permanent_event_error_does_not_rollback_sibling_task_writes(postgres_store) -> None:
    repo = postgres_store.job_repo
    invalid_task_id = f"stage-history-invalid-{uuid4()}"
    valid_task_id = f"stage-history-valid-{uuid4()}"
    for task_id in (invalid_task_id, valid_task_id):
        await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))

    invalid_event = _events(invalid_task_id)[1].model_copy(update={"duration_ms": 2**63})
    valid_events = _events(valid_task_id)
    result = await repo.write_stage_progress(
        events=[invalid_event, *valid_events],
        summaries=[_summary(invalid_task_id), _summary(valid_task_id)],
    )

    assert result.permanent_failed_task_ids == [invalid_task_id]
    assert [row.task_id for row in result.coverage] == [valid_task_id]
    assert await repo.get_stage_progress_summaries([invalid_task_id]) == {}
    valid_state = (await repo.get_stage_progress_summaries([valid_task_id]))[valid_task_id]
    assert valid_state.persisted_through_sequence == 2
    assert [event.sequence for event in (await repo.get_stage_progress_events(valid_task_id, limit=100)).events] == [
        1,
        2,
    ]

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = ANY($1::text[])", [invalid_task_id, valid_task_id])


async def test_permanent_summary_error_persists_sanitized_loss_marker(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-invalid-summary-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))
    invalid_summary = _summary(task_id).model_copy(update={"current_stage_id": "x" * 65})
    writer = StageEventWriter(repo)

    assert await writer._write_batch([], [invalid_summary]) is True
    loss_marker = writer.pending_summaries[task_id]
    assert loss_marker.known_loss is True
    assert loss_marker.current_stage_id is None

    await writer._write_batch([], [writer._summaries.pop(task_id)])

    persisted = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert persisted.known_loss is True
    assert persisted.current_stage_id is None
    assert persisted.phase is StageTaskPhase.PREPARING
    assert persisted.latest_sequence == invalid_summary.latest_sequence

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
    conflict_state = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert conflict_state.known_conflict is True
    assert conflict_state.latest_sequence == 0
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
    conflict_state = (await repo.get_stage_progress_summaries([previous_attempt_task_id]))[previous_attempt_task_id]
    assert conflict_state.known_conflict is True
    assert conflict_state.latest_sequence == 0
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


async def test_stage_summary_persists_parser_substage_progress(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"parser-substage-{uuid4()}"
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
    summary = StageProgressSummary(
        task_id=task_id,
        attempt=1,
        latest_sequence=1,
        snapshot_revision=2,
        phase=StageTaskPhase.STAGE,
        current_stage_id="parse",
        current_stage_rank=0,
        current_substage_id="parse.text",
        current_substage_label="Extracting page text",
        current_substage_progress=42,
        current_substage_total=578,
        stage_started_at=started_at,
        current_invocation_id=invocation_id,
        has_open_invocation=True,
    )

    await repo.write_stage_progress(events=[started], summaries=[summary])
    persisted = (await repo.get_stage_progress_summaries([task_id]))[task_id]

    assert persisted.current_substage_id == "parse.text"
    assert persisted.current_substage_label == "Extracting page text"
    assert persisted.current_substage_progress == 42
    assert persisted.current_substage_total == 578
    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_confirmed_failure_marks_open_invocation_and_late_open_snapshot_keeps_marker(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-interrupted-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.FAILED, partition="test"))

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
    open_summary = StageProgressSummary(
        task_id=task_id,
        attempt=1,
        latest_sequence=1,
        snapshot_revision=1,
        phase=StageTaskPhase.STAGE,
        current_stage_id="parse",
        current_stage_rank=0,
        stage_started_at=started_at,
        current_invocation_id=invocation_id,
        has_open_invocation=True,
    )
    await repo.write_stage_progress(events=[started], summaries=[open_summary])

    assert await repo.mark_stage_progress_interrupted(task_id) is True
    assert await repo.mark_stage_progress_interrupted(task_id) is False
    assert (
        await postgres_store.pool.fetchval(
            "SELECT stage_interruption_confirmed_attempt FROM jobs WHERE id = $1", task_id
        )
        == 1
    )
    assert (await repo.get_stage_progress_summaries([task_id]))[task_id].interrupted_invocation_id == invocation_id

    # A queued worker snapshot can arrive after the completion tracker writes
    # the settlement marker. It must not erase the interruption evidence.
    await repo.write_stage_progress(events=[], summaries=[open_summary.model_copy(update={"snapshot_revision": 2})])
    still_marked = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert still_marked.interrupted_invocation_id == invocation_id

    completed = StageProgressEvent(
        task_id=task_id,
        attempt=1,
        sequence=2,
        invocation_id=invocation_id,
        stage_id="parse",
        stage_rank=0,
        status=StageEventStatus.COMPLETED,
        occurred_at=datetime.now(UTC),
        duration_ms=125,
    )
    closed_summary = StageProgressSummary(
        task_id=task_id,
        attempt=1,
        latest_sequence=2,
        snapshot_revision=3,
        phase=StageTaskPhase.BETWEEN_STAGES,
        current_stage_rank=0,
        expected_final_sequence=2,
    )
    await repo.write_stage_progress(events=[completed], summaries=[closed_summary])
    assert (await repo.get_stage_progress_summaries([task_id]))[task_id].interrupted_invocation_id is None

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_settlement_before_first_snapshot_and_retry_track_the_confirmed_attempt(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-late-snapshot-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.FAILED, partition="test"))

    # The worker can settle before its queued first stage snapshot reaches Postgres.
    assert await repo.mark_stage_progress_interrupted(task_id) is False
    assert (
        await postgres_store.pool.fetchval(
            "SELECT stage_interruption_confirmed_attempt FROM jobs WHERE id = $1", task_id
        )
        == 1
    )

    def open_attempt(attempt: int, invocation_id):
        started = StageProgressEvent(
            task_id=task_id,
            attempt=attempt,
            sequence=1,
            invocation_id=invocation_id,
            stage_id="parse",
            stage_rank=0,
            status=StageEventStatus.STARTED,
            occurred_at=datetime.now(UTC),
        )
        summary = StageProgressSummary(
            task_id=task_id,
            attempt=attempt,
            latest_sequence=1,
            snapshot_revision=1,
            phase=StageTaskPhase.STAGE,
            current_stage_id="parse",
            current_stage_rank=0,
            current_invocation_id=invocation_id,
            has_open_invocation=True,
        )
        return started, summary

    first_id = uuid4()
    first_start, first_summary = open_attempt(1, first_id)
    await repo.write_stage_progress(events=[first_start], summaries=[first_summary])
    first = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert first.interrupted_invocation_id == first_id

    retry_id = uuid4()
    retry_start, retry_summary = open_attempt(2, retry_id)
    await repo.write_stage_progress(events=[retry_start], summaries=[retry_summary])
    retried = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert retried.attempt == 2
    assert retried.interrupted_invocation_id is None

    # A later retry can fail too. Its settlement must advance the marker so
    # the second invocation is not left looking merely like the old snapshot.
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.SERIALIZING, partition="test"))
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.FAILED, partition="test"))
    assert await repo.mark_stage_progress_interrupted(task_id) is True
    assert (
        await postgres_store.pool.fetchval(
            "SELECT stage_interruption_confirmed_attempt FROM jobs WHERE id = $1", task_id
        )
        == 2
    )
    retried = (await repo.get_stage_progress_summaries([task_id]))[task_id]
    assert retried.interrupted_invocation_id == retry_id

    await postgres_store.pool.execute("DELETE FROM jobs WHERE id = $1", task_id)


async def test_settled_failure_does_not_mark_invocation_with_terminal_event(postgres_store) -> None:
    repo = postgres_store.job_repo
    task_id = f"stage-history-closed-{uuid4()}"
    await repo.upsert_job(IndexationJob(id=task_id, status=DocumentStatus.FAILED, partition="test"))
    started, completed = _events(task_id)
    open_summary = StageProgressSummary(
        task_id=task_id,
        attempt=1,
        latest_sequence=2,
        snapshot_revision=2,
        phase=StageTaskPhase.STAGE,
        current_stage_id="parse",
        current_stage_rank=0,
        stage_started_at=started.occurred_at,
        current_invocation_id=started.invocation_id,
        has_open_invocation=True,
    )
    await repo.write_stage_progress(events=[started, completed], summaries=[open_summary])

    assert await repo.mark_stage_progress_interrupted(task_id) is False
    assert (await repo.get_stage_progress_summaries([task_id]))[task_id].interrupted_invocation_id is None

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


async def test_followup_migration_backfills_jobs_settled_after_stage_history_upgrade(legacy_jobs_db) -> None:
    connection, cfg = legacy_jobs_db
    await asyncio.to_thread(command.upgrade, cfg, _STAGE_HISTORY_REVISION)
    await connection.execute(
        """
        INSERT INTO jobs (id, partition, status, stage_tracking_version)
        VALUES
            ('gap-complete', 'p', 'SERIALIZING', NULL),
            ('gap-active', 'p', 'SERIALIZING', NULL),
            ('tracked-complete', 'p', 'SERIALIZING', 1)
        """
    )
    await connection.execute("UPDATE jobs SET status = 'COMPLETED' WHERE id = 'gap-complete'")
    await connection.execute("UPDATE jobs SET status = 'COMPLETED' WHERE id = 'tracked-complete'")

    await asyncio.to_thread(command.upgrade, cfg, _STAGE_TRACKING_BACKFILL_REVISION)

    jobs = await connection.fetch("SELECT id, stage_tracking_version FROM jobs ORDER BY id")
    assert [(row["id"], row["stage_tracking_version"]) for row in jobs] == [
        ("gap-active", None),
        ("gap-complete", 0),
        ("legacy-active", None),
        ("legacy-complete", 0),
        ("tracked-complete", 1),
    ]
