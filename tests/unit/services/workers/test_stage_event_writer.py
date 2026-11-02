from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import services.workers.stage_event_writer as stage_event_writer_module
from core.models.stage_progress import (
    StageEventStatus,
    StageProgressCoverage,
    StageProgressEvent,
    StageProgressSummary,
    StageProgressWorkerSnapshot,
    StageProgressWriteResult,
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
async def test_writer_gives_single_task_a_one_second_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = StageEventWriter(_Repo(), write_timeout=0.075)
    observed_timeouts = []
    original_wait_for = asyncio.wait_for

    async def observe_wait_for(awaitable, *, timeout):
        observed_timeouts.append(timeout)
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(stage_event_writer_module.asyncio, "wait_for", observe_wait_for)
    await writer._write_batch([_transition("task-1", 1)], [_summary("task-1", 1)])

    assert observed_timeouts == [pytest.approx(1.0)]


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
async def test_failed_terminal_summary_is_retried_with_known_loss_after_recovery() -> None:
    task_id = "terminal-retry"

    class RecoveringRepo:
        def __init__(self) -> None:
            self.remaining_failures = 2
            self.persisted_summaries = []

        async def write_stage_progress(self, *, events, summaries):
            if self.remaining_failures:
                self.remaining_failures -= 1
                raise RuntimeError("database unavailable")
            self.persisted_summaries.extend(summaries)
            return StageProgressWriteResult(
                coverage=[
                    StageProgressCoverage(
                        task_id=summary.task_id,
                        attempt=summary.attempt,
                        persisted_through_sequence=summary.expected_final_sequence or 0,
                    )
                    for summary in summaries
                ]
            )

    repo = RecoveringRepo()
    writer = StageEventWriter(repo, retry_delay=0, terminal_retry_delay=0)
    key = (task_id, 1)
    summary = _summary(task_id, 2).model_copy(update={"expected_final_sequence": 2})
    writer._terminal[task_id] = (
        writer._monotonic() + writer._terminal_ttl,
        StageProgressWorkerSnapshot(summary=summary),
    )
    writer._pending_ack[key] = None

    await writer._write_batch([], [summary])

    retry_summary = writer.pending_summaries[task_id]
    assert retry_summary.known_loss is True
    await writer._write_batch([], [writer._summaries.pop(task_id)])

    assert repo.persisted_summaries[-1].known_loss is True
    assert key not in writer._pending_ack


@pytest.mark.asyncio
async def test_conflicted_persisted_history_counts_as_incomplete_after_coverage_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "conflicted-task"

    class ConflictedRepo:
        async def write_stage_progress(self, *, events, summaries):
            return StageProgressWriteResult(
                coverage=[StageProgressCoverage(task_id=task_id, attempt=1, persisted_through_sequence=1)],
                conflicted_task_ids=[task_id],
            )

    writer = StageEventWriter(ConflictedRepo())
    key = (task_id, 1)
    writer._pending_ack[key] = None
    observed_metrics = []
    monkeypatch.setattr(
        stage_event_writer_module,
        "set_stage_history_counts",
        lambda **counts: observed_metrics.append(counts),
    )

    await writer._write_batch([_transition(task_id, 1)], [_summary(task_id, 1)])
    writer._refresh_metrics()

    assert key not in writer._pending_ack
    assert task_id in writer._known_incomplete
    assert observed_metrics[-1] == {"pending": 0, "incomplete": 1}


@pytest.mark.asyncio
async def test_writer_marks_batch_lost_when_cancelled_during_retry_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(error=RuntimeError("database unavailable"))
    writer = StageEventWriter(repo, retry_delay=1.0)
    tracker = StageTracker("task-1")
    tracker.start_stage("parse", 0)
    writer._trackers[tracker.task_id] = tracker
    retry_started = asyncio.Event()
    drops = []

    async def wait_for_retry(_delay):
        retry_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(stage_event_writer_module.asyncio, "sleep", wait_for_retry)
    monkeypatch.setattr(
        stage_event_writer_module,
        "record_stage_event_drop",
        lambda reason, count=1: drops.append((reason, count)),
    )
    task = asyncio.create_task(writer._write_batch([_transition("task-1", 1)], [_summary("task-1", 1)]))
    await asyncio.wait_for(retry_started.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert writer.dropped_events == 1
    assert writer.write_retries == 1
    assert writer.write_failures == 0
    assert tracker.summary().known_loss is True
    assert "task-1" in writer._known_incomplete
    assert drops == [("shutdown", 1)]


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
