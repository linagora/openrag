from __future__ import annotations

from datetime import UTC, datetime

from core.models.stage_progress import StageEventStatus, StageFailureEffect, StageTaskPhase
from services.workers.stage_tracker import StageTracker


class _Clock:
    def __init__(self) -> None:
        self.elapsed = 100.0

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


def _event_deliveries(task_id: str = "task-1"):
    deliveries = []

    def deliver(event, summary, tail):
        deliveries.append((event, summary, tuple(tail)))
        return True

    return deliveries, deliver


def test_complete_seven_stage_attempt_keeps_all_transitions_in_each_snapshot() -> None:
    clock = _Clock()
    deliveries, deliver = _event_deliveries()
    tracker = StageTracker(
        "task-1",
        deliver=deliver,
        monotonic=clock.monotonic,
        utcnow=lambda: datetime(2026, 10, 22, tzinfo=UTC),
    )

    for rank, stage_id in enumerate(("parse", "caption", "chunk", "contextualize", "topic_tag", "embed", "store")):
        tracker.start_stage(stage_id, rank)
        clock.advance(0.125)
        tracker.finish_stage()

    events = [event for event, _, _ in deliveries if event is not None]
    assert len(events) == 14
    assert [event.sequence for event in events] == list(range(1, 15))
    assert [event.status for event in events] == [
        status for _ in range(7) for status in (StageEventStatus.STARTED, StageEventStatus.COMPLETED)
    ]
    assert len({event.invocation_id for event in events}) == 7
    assert {event.duration_ms for event in events if event.status is StageEventStatus.COMPLETED} == {125}
    assert deliveries[-1][2] == tuple(events)
    assert deliveries[-1][1].phase is StageTaskPhase.BETWEEN_STAGES
    assert deliveries[-1][1].latest_sequence == 14


def test_degradable_failure_is_an_observed_failed_stage_and_pipeline_can_continue() -> None:
    deliveries, deliver = _event_deliveries()
    tracker = StageTracker("task-1", deliver=deliver)
    tracker.start_stage("contextualize", 3)
    tracker.finish_stage(error=RuntimeError("private failure detail"), degraded=True)

    start, failure = [event for event, _, _ in deliveries if event is not None]
    assert start.status is StageEventStatus.STARTED
    assert failure.status is StageEventStatus.FAILED
    assert failure.failure_effect is StageFailureEffect.CONTINUED_DEGRADED
    assert failure.duration_ms is not None
    assert "private failure detail" not in repr(failure.model_dump())
    assert deliveries[-1][1].phase is StageTaskPhase.BETWEEN_STAGES
    assert deliveries[-1][1].has_open_invocation is False


def test_cancellation_closes_only_an_invocation_that_was_started() -> None:
    deliveries, deliver = _event_deliveries()
    tracker = StageTracker("task-1", deliver=deliver)
    assert tracker.snapshot().event_tail == []

    tracker.start_stage("embed", 5)
    tracker.finish_stage(cancelled=True)

    events = [event for event, _, _ in deliveries if event is not None]
    assert [event.status for event in events] == [StageEventStatus.STARTED, StageEventStatus.CANCELLED]
    assert events[1].duration_ms is not None
    assert deliveries[-1][1].has_open_invocation is False


def test_tracking_delivery_failure_is_captured_without_raising() -> None:
    calls = 0

    def fail_delivery(event, summary, tail):
        nonlocal calls
        calls += 1
        raise RuntimeError("writer unavailable")

    tracker = StageTracker("task-1", deliver=fail_delivery)
    tracker.start_stage("parse", 0)
    tracker.finish_stage()
    assert calls == 3
    assert tracker.summary().known_loss is True


def test_invalid_monotonic_duration_is_left_unknown() -> None:
    readings = iter((10.0, 9.0))
    deliveries, deliver = _event_deliveries()
    tracker = StageTracker("task-1", deliver=deliver, monotonic=lambda: next(readings))
    tracker.start_stage("parse", 0)
    end = tracker.finish_stage()

    assert end is not None
    assert end.duration_ms is None
