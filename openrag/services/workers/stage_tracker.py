"""Task-local, best-effort observations of actual indexing stage boundaries."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from core.models.stage_progress import (
    StageEventStatus,
    StageFailureEffect,
    StageProgressEvent,
    StageProgressSummary,
    StageProgressWorkerSnapshot,
    StageTaskPhase,
)

MAX_STAGE_EVENT_TAIL = 32


class StageTracker:
    """Build immutable transitions and a cumulative, bounded live snapshot.

    ``deliver`` must be synchronous and non-blocking. It receives the newly
    observed event (or ``None`` for a phase/final marker), the compact summary,
    and the current-attempt cumulative tail. Delivery failures only set the
    local loss bit; they never escape into the indexing pipeline.
    """

    def __init__(
        self,
        task_id: str,
        *,
        attempt: int = 1,
        deliver: Callable[
            [StageProgressEvent | None, StageProgressSummary, tuple[StageProgressEvent, ...]], bool | None
        ]
        | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.task_id = task_id
        self.attempt = attempt
        self._deliver = deliver
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._sequence = 0
        self._revision = 0
        self._phase = StageTaskPhase.PREPARING
        self._current_stage_id: str | None = None
        self._current_stage_rank: int | None = None
        self._current_invocation_id: UUID | None = None
        self._stage_started_at: datetime | None = None
        self._stage_started_monotonic: float | None = None
        self._position_score = 0
        self._tail: list[StageProgressEvent] = []
        self._expected_final_sequence: int | None = None
        self._known_loss = False
        self._elapsed_ms: int | None = None
        self._elapsed_sampled_at: datetime | None = None
        self._finished = False

    def start_stage(self, stage_id: str, stage_rank: int) -> StageProgressEvent:
        """Record the moment a real awaited stage begins."""
        if self._finished:
            raise RuntimeError("cannot enter a stage after task tracking has finished")
        if self._current_invocation_id is not None:
            raise RuntimeError("cannot start a stage while another stage invocation is active")
        self._current_invocation_id = uuid4()
        self._current_stage_id = stage_id
        self._current_stage_rank = stage_rank
        self._stage_started_at = _aware_utc(self._utcnow())
        self._stage_started_monotonic = self._monotonic()
        self._elapsed_ms = 0
        self._elapsed_sampled_at = self._stage_started_at
        self._phase = StageTaskPhase.STAGE
        self._position_score = stage_rank * 1000
        return self._record(StageEventStatus.STARTED, duration_ms=None)

    def finish_stage(
        self,
        *,
        error: BaseException | None = None,
        degraded: bool = False,
        cancelled: bool = False,
    ) -> StageProgressEvent | None:
        """Close the active invocation using an observed result only."""
        if self._current_invocation_id is None or self._current_stage_id is None:
            return None
        if cancelled:
            status = StageEventStatus.CANCELLED
            failure_effect = None
        elif error is not None:
            status = StageEventStatus.FAILED
            failure_effect = StageFailureEffect.CONTINUED_DEGRADED if degraded else StageFailureEffect.TASK_FAILED
        else:
            status = StageEventStatus.COMPLETED
            failure_effect = None

        duration_ms = self._duration_ms()
        event = self._record(status, duration_ms=duration_ms, failure_effect=failure_effect)
        self._phase = StageTaskPhase.BETWEEN_STAGES
        if self._current_stage_rank is not None:
            self._position_score = self._current_stage_rank * 1000 + 500
        self._current_stage_id = None
        self._current_stage_rank = None
        self._current_invocation_id = None
        self._stage_started_at = None
        self._stage_started_monotonic = None
        self._elapsed_ms = None
        self._elapsed_sampled_at = None
        self._revision += 1
        self._publish(None)
        return event

    def set_phase(self, phase: StageTaskPhase, *, position_score: int | None = None) -> None:
        """Update a task phase that is outside a canonical pipeline stage."""
        if self._current_invocation_id is not None and phase is not StageTaskPhase.STAGE:
            raise RuntimeError("cannot leave the stage phase while an invocation is active")
        previous = (self._phase, self._position_score)
        self._phase = phase
        if position_score is not None:
            self._position_score = position_score
        elif phase is StageTaskPhase.FINALIZING:
            self._position_score = 7000
        if previous != (self._phase, self._position_score):
            self._revision += 1
        self._publish(None)

    def finish_task(self) -> None:
        """Mark the final observed sequence; this does not fabricate a stage end."""
        if self._finished:
            return
        self._finished = True
        self._expected_final_sequence = self._sequence
        self._revision += 1
        self._publish(None)

    def summary(self) -> StageProgressSummary:
        return StageProgressSummary(
            task_id=self.task_id,
            attempt=self.attempt,
            latest_sequence=self._sequence,
            snapshot_revision=self._revision,
            phase=self._phase,
            current_stage_id=self._current_stage_id,
            current_stage_rank=self._current_stage_rank,
            position_score=self._position_score,
            stage_started_at=self._stage_started_at,
            current_invocation_id=self._current_invocation_id,
            expected_final_sequence=self._expected_final_sequence,
            known_loss=self._known_loss,
            has_open_invocation=self._current_invocation_id is not None,
        )

    def snapshot(self) -> StageProgressWorkerSnapshot:
        return StageProgressWorkerSnapshot(
            summary=self.summary(),
            event_tail=list(self._tail),
            active_invocation_id=self._current_invocation_id,
            elapsed_sampled_at=self._elapsed_sampled_at,
            elapsed_ms=self._elapsed_ms,
        )

    def sample_elapsed(self) -> StageProgressWorkerSnapshot | None:
        """Return one worker-monotonic sample for the current stage, if valid."""
        if self._current_invocation_id is None:
            return None
        self._revision += 1
        self._elapsed_ms = self._duration_ms()
        self._elapsed_sampled_at = _aware_utc(self._utcnow())
        return self.snapshot()

    def mark_loss(self) -> None:
        """Remember a known delivery loss for the next surviving snapshot."""
        self._mark_loss()

    def _duration_ms(self) -> int | None:
        if self._stage_started_monotonic is None:
            return None
        elapsed = self._monotonic() - self._stage_started_monotonic
        if not math.isfinite(elapsed) or elapsed < 0:
            return None
        return int(elapsed * 1000)

    def _record(
        self,
        status: StageEventStatus,
        *,
        duration_ms: int | None,
        failure_effect: StageFailureEffect | None = None,
    ) -> StageProgressEvent:
        self._sequence += 1
        event = StageProgressEvent(
            task_id=self.task_id,
            attempt=self.attempt,
            sequence=self._sequence,
            invocation_id=self._current_invocation_id or uuid4(),
            stage_id=self._current_stage_id or "unknown",
            stage_rank=self._current_stage_rank,
            status=status,
            occurred_at=_aware_utc(self._utcnow()),
            duration_ms=duration_ms,
            failure_effect=failure_effect,
        )
        self._tail.append(event)
        if len(self._tail) > MAX_STAGE_EVENT_TAIL:
            self._tail.pop(0)
        self._revision += 1
        self._publish(event)
        return event

    def _publish(self, event: StageProgressEvent | None) -> None:
        if self._deliver is None:
            return
        try:
            accepted = self._deliver(event, self.summary(), tuple(self._tail))
            if accepted is False:
                self._mark_loss()
        except Exception:  # noqa: BLE001 - tracking must never affect indexing
            self._mark_loss()

    def _mark_loss(self) -> None:
        if not self._known_loss:
            self._known_loss = True
            self._revision += 1


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stage tracker clock must return an aware datetime")
    return value.astimezone(UTC)
