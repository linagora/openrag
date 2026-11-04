"""Schema and repository contracts for per-task stage history."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
from core.models.stage_progress import StageEventStatus, StageProgressEvent, StageProgressSummary, StageTaskPhase
from services.persistence.job_repo import PgJobRepository
from services.persistence.schema import metadata


def test_stage_history_schema_has_bounded_task_scoped_storage() -> None:
    events = metadata.tables.get("job_stage_events")
    state = metadata.tables.get("job_stage_state")

    assert events is not None
    assert state is not None
    (task_foreign_key,) = events.c.task_id.foreign_keys
    assert task_foreign_key.ondelete == "CASCADE"
    assert {"event_id", "attempt", "sequence", "invocation_id", "status"} <= set(events.c.keys())
    assert {
        "attempt",
        "snapshot_revision",
        "interrupted_invocation_id",
        "persisted_through_sequence",
        "expected_final_sequence",
    } <= set(state.c.keys())
    assert "has_summary" in state.c
    assert "stage_tracking_version" in metadata.tables["jobs"].c


def test_stage_history_schema_enforces_unique_sequence_and_one_invocation_outcome() -> None:
    events = metadata.tables.get("job_stage_events")

    assert events is not None
    constraints = {tuple(column.name for column in constraint.columns) for constraint in events.constraints}
    indexes = {index.name: index for index in events.indexes}

    assert ("task_id", "attempt", "sequence") in constraints
    assert "uq_job_stage_events_one_start" in indexes
    assert "uq_job_stage_events_one_terminal" in indexes
    assert str(indexes["uq_job_stage_events_one_start"].dialect_options["postgresql"]["where"]) == "status = 'started'"
    assert "status IN" in str(indexes["uq_job_stage_events_one_terminal"].dialect_options["postgresql"]["where"])


@pytest.mark.asyncio
async def test_stage_writer_rejects_oversized_batches_before_acquiring_storage() -> None:
    repo = PgJobRepository(lambda: None)
    event = StageProgressEvent(
        task_id="task-1",
        attempt=1,
        sequence=1,
        invocation_id=uuid4(),
        stage_id="parse",
        stage_rank=0,
        status=StageEventStatus.STARTED,
        occurred_at=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="at most 32 stage events"):
        await repo.write_stage_progress(events=[event] * 33, summaries=[])


@pytest.mark.asyncio
async def test_stage_writer_locks_the_task_row_before_reading_event_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.persistence import job_repo as job_repo_module

    calls: list[tuple[str, str, tuple[object, ...]]] = []

    class _Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _Connection:
        def transaction(self):
            return _Context()

        async def fetchrow(self, query: str, *args: object):
            calls.append(("fetchrow", query, args))
            return {"id": args[0]}

        async def execute(self, query: str, *args: object):
            calls.append(("execute", query, args))
            return "UPDATE 1"

    class _Pool:
        def acquire(self):
            return _ContextWithValue(_Connection())

    class _ContextWithValue(_Context):
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

    async def no_rows(*_args):
        return 0

    async def no_conflict(*_args):
        return False

    async def no_summary_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(job_repo_module, "_persisted_through_sequence", no_rows)
    monkeypatch.setattr(job_repo_module, "_has_conflicting_invocation_pair", no_conflict)
    monkeypatch.setattr(job_repo_module, "_upsert_stage_summary", no_summary_write)
    repo = PgJobRepository(lambda: _Pool())
    task_id = "task-locked"
    summary = StageProgressSummary(
        task_id=task_id,
        attempt=1,
        latest_sequence=0,
        snapshot_revision=0,
        phase=StageTaskPhase.PREPARING,
    )

    await repo.write_stage_progress(events=[], summaries=[summary])

    assert calls[0][0] == "fetchrow"
    assert "SELECT id FROM jobs WHERE id = $1 FOR UPDATE" in calls[0][1]
    assert calls[0][2] == (task_id,)


@pytest.mark.asyncio
async def test_permanent_task_error_does_not_roll_back_other_task_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.persistence import job_repo as job_repo_module

    inserted_tasks: list[str] = []
    persisted_summaries: list[str] = []

    class _Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _Transaction:
        async def __aenter__(self):
            self.inserted_count = len(inserted_tasks)
            self.summary_count = len(persisted_summaries)
            return self

        async def __aexit__(self, exc_type, *_args):
            if exc_type is not None:
                del inserted_tasks[self.inserted_count :]
                del persisted_summaries[self.summary_count :]
            return False

    class _Connection:
        def transaction(self):
            return _Transaction()

        async def fetchrow(self, _query: str, *args: object):
            return {"id": args[0]}

        async def execute(self, _query: str, *_args: object):
            return "UPDATE 1"

    class _ContextWithValue(_Context):
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

    class _Pool:
        def acquire(self):
            return _ContextWithValue(_Connection())

    def event_for(task_id: str, sequence: int = 1) -> StageProgressEvent:
        return StageProgressEvent(
            task_id=task_id,
            attempt=1,
            sequence=sequence,
            invocation_id=uuid4(),
            stage_id="parse",
            status=StageEventStatus.STARTED,
            occurred_at=datetime.now(UTC),
        )

    def summary_for(task_id: str) -> StageProgressSummary:
        return StageProgressSummary(task_id=task_id)

    async def insert_event(_connection, event):
        if event.task_id == "invalid-task" and event.sequence == 2:
            raise asyncpg.NumericValueOutOfRangeError("duration is outside BIGINT range")
        inserted_tasks.append(event.task_id)
        return False

    async def persisted_through(_connection, _task_id: str, _attempt: int) -> int:
        return 1

    async def no_conflict(_connection, _task_id: str, _attempt: int) -> bool:
        return False

    async def persist_summary(_connection, summary, *, persisted_through_sequence: int) -> None:
        persisted_summaries.append(summary.task_id)

    monkeypatch.setattr(job_repo_module, "_insert_stage_event", insert_event)
    monkeypatch.setattr(job_repo_module, "_persisted_through_sequence", persisted_through)
    monkeypatch.setattr(job_repo_module, "_has_conflicting_invocation_pair", no_conflict)
    monkeypatch.setattr(job_repo_module, "_upsert_stage_summary", persist_summary)
    repo = PgJobRepository(lambda: _Pool())

    result = await repo.write_stage_progress(
        events=[event_for("invalid-task", 1), event_for("invalid-task", 2), event_for("valid-task")],
        summaries=[summary_for("invalid-task"), summary_for("valid-task")],
    )

    assert result.permanent_failed_task_ids == ["invalid-task"]
    assert [row.task_id for row in result.coverage] == ["valid-task"]
    assert inserted_tasks == ["valid-task"]
    assert persisted_summaries == ["valid-task"]


@pytest.mark.asyncio
async def test_stage_history_reader_rejects_unbounded_page_size_before_storage_access() -> None:
    repo = PgJobRepository(lambda: None)

    with pytest.raises(ValueError, match="between 1 and 200"):
        await repo.get_stage_progress_events("task-1", limit=201)
