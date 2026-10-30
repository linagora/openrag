from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import services.workers.stage_event_writer as stage_event_writer_module
from core.models.stage_progress import (
    StageEventStatus,
    StageProgressEvent,
    StageProgressSummary,
    StageTaskPhase,
)
from services.workers.stage_event_writer import StageEventWriter
from services.workers.stage_tracker import StageTracker


def _transition(task_id: str, sequence: int) -> StageProgressEvent:
    return StageProgressEvent(
        task_id=task_id,
        attempt=1,
        sequence=sequence,
        invocation_id=uuid4(),
        stage_id="parse",
        stage_rank=0,
        status=StageEventStatus.STARTED,
        occurred_at=datetime.now(UTC),
    )


def _summary(task_id: str, sequence: int = 0) -> StageProgressSummary:
    return StageProgressSummary(
        task_id=task_id,
        attempt=1,
        latest_sequence=sequence,
        snapshot_revision=sequence,
        phase=StageTaskPhase.PREPARING,
    )


class _Repo:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = []

    async def write_stage_progress(self, *, events, summaries):
        self.calls.append((list(events), list(summaries)))
        if self.error is not None:
            raise self.error
        return None


@pytest.mark.asyncio
async def test_writer_bounds_event_queue_and_marks_known_drop() -> None:
    repo = _Repo()
    writer = StageEventWriter(repo, queue_capacity=1, flush_interval=10)

    assert writer.publish(_transition("one", 1), _summary("one", 1), ()) is True
    assert writer.publish(_transition("two", 1), _summary("two", 1), ()) is False
    assert writer.queued_event_count == 1
    assert writer.dropped_events == 1
    assert writer.pending_summaries["two"].known_loss is True

    await writer.close()
    assert sum(len(events) for events, _ in repo.calls) == 1


@pytest.mark.asyncio
async def test_writer_batches_transition_events_and_keeps_latest_summary() -> None:
    repo = _Repo()
    writer = StageEventWriter(repo, batch_size=32, flush_interval=10)
    tracker = StageTracker("task-1", deliver=writer.publish)
    writer.register_tracker(tracker)
    tracker.start_stage("parse", 0)
    tracker.finish_stage()
    tracker.start_stage("chunk", 2)
    tracker.finish_stage()

    await writer.close()

    events = [event for batch, _ in repo.calls for event in batch]
    summaries = [summary for _, rows in repo.calls for summary in rows]
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert len(summaries) == 1
    assert summaries[0].latest_sequence == 4
    assert summaries[0].phase is StageTaskPhase.BETWEEN_STAGES


@pytest.mark.asyncio
async def test_writer_retries_once_then_drops_without_raising() -> None:
    repo = _Repo(error=RuntimeError("database unavailable"))
    writer = StageEventWriter(repo, flush_interval=10, retry_delay=0.001)
    writer.publish(_transition("task-1", 1), _summary("task-1", 1), ())

    await writer.close()

    assert len(repo.calls) == 2
    assert writer.write_failures == 1
    assert writer.dropped_events == 1


@pytest.mark.asyncio
async def test_writer_scales_write_deadline_with_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo()
    writer = StageEventWriter(repo, batch_size=32, write_timeout=0.075)
    events = [_transition(f"task-{index}", 1) for index in range(32)]
    summaries = [_summary(f"task-{index}", 1) for index in range(32)]
    observed_timeouts = []
    original_wait_for = asyncio.wait_for

    async def observe_wait_for(awaitable, *, timeout):
        observed_timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(stage_event_writer_module.asyncio, "wait_for", observe_wait_for)

    await writer._write_batch(events, summaries)

    assert observed_timeouts == [pytest.approx(2.4)]


@pytest.mark.asyncio
async def test_writer_counts_summary_only_write_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(error=RuntimeError("database unavailable"))
    writer = StageEventWriter(repo, retry_delay=0.001)
    drops = []
    monkeypatch.setattr(
        stage_event_writer_module,
        "record_stage_event_drop",
        lambda reason, count=1: drops.append((reason, count)),
    )

    await writer._write_batch([], [_summary("summary-only")])

    assert drops == [("write_failed", 1)]


@pytest.mark.asyncio
async def test_elapsed_sampler_updates_live_snapshot_without_durable_event() -> None:
    repo = _Repo()
    samples = []
    writer = StageEventWriter(repo, live_sink=lambda snapshot: _append(samples, snapshot), flush_interval=10)
    now = [100.0]
    tracker = StageTracker("task-1", deliver=writer.publish, monotonic=lambda: now[0])
    writer.register_tracker(tracker)
    tracker.start_stage("embed", 5)
    now[0] = 101.25

    await writer.sample_active_once()
    await writer.close()

    measured = [sample for sample in samples if sample.elapsed_ms == 1250]
    assert measured
    assert measured[0].active_invocation_id == tracker.summary().current_invocation_id
    assert sum(len(events) for events, _ in repo.calls) == 1


@pytest.mark.asyncio
async def test_shutdown_drain_is_bounded_when_repository_write_never_returns() -> None:
    class StuckRepo:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def write_stage_progress(self, *, events, summaries):
            self.started.set()
            await asyncio.Event().wait()

    repo = StuckRepo()
    writer = StageEventWriter(
        repo,
        flush_interval=10,
        write_timeout=1.0,
        shutdown_timeout=0.01,
    )
    writer.publish(_transition("task-1", 1), _summary("task-1", 1), ())
    await asyncio.wait_for(repo.started.wait(), timeout=0.1)
    started = asyncio.get_running_loop().time()

    await writer.close()

    assert asyncio.get_running_loop().time() - started < 0.1
    assert writer.dropped_events == 1


async def _append(target, value):
    target.append(value)
