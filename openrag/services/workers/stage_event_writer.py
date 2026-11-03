"""Bounded best-effort delivery for worker-observed indexing stage progress."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from core.models.stage_progress import (
    StageProgressEvent,
    StageProgressSummary,
    StageProgressWorkerSnapshot,
    StageProgressWriteResult,
    StageTaskPhase,
)
from core.observability.ray_metrics import (
    record_stage_event_drop,
    record_stage_event_write,
    record_stage_live_snapshot_failure,
    set_stage_event_queue_size,
    set_stage_history_counts,
)
from core.utils.logging import get_logger
from services.workers.stage_tracker import StageTracker

logger = get_logger()
_MIN_STAGE_WRITE_TIMEOUT = 1.0
_PERMANENT_POSTGRES_ERROR_CLASSES = frozenset({"22", "23"})
_MAX_POSTGRES_INTEGER = 2**31 - 1
_MAX_POSTGRES_BIGINT = 2**63 - 1
_MAX_SINGLE_EVENT_TIMEOUTS = 3


def _is_permanent_write_error(error: Exception) -> bool:
    """Return whether Postgres rejected the batch for invalid data or a constraint."""
    sqlstate = getattr(error, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate[:2] in _PERMANENT_POSTGRES_ERROR_CLASSES


class StageEventWriter:
    """Own one bounded event queue and coalesced live mailbox per worker actor.

    ``publish`` is synchronous and uses only ``put_nowait``-style operations.
    PostgreSQL and TaskStateManager calls happen in background tasks with
    explicit deadlines. Transient PostgreSQL failures are retained in bounded
    queues and retried with capped backoff. Permanent data or constraint errors
    are dropped and recorded as incomplete history.
    """

    def __init__(
        self,
        job_repo: Any,
        *,
        live_sink: Callable[[StageProgressWorkerSnapshot], Any] | None = None,
        queue_capacity: int = 512,
        batch_size: int = 32,
        flush_interval: float = 0.05,
        write_timeout: float = 0.075,
        retry_delay: float = 0.025,
        retry_max_delay: float = 1.0,
        shutdown_timeout: float = 6.0,
        sample_interval: float = 1.0,
        live_timeout: float = 0.075,
        live_retry_delay: float = 0.025,
        max_live_calls: int = 8,
        terminal_capacity: int = 32,
        terminal_ttl: float = 30.0,
        max_active_tasks: int = 1,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if queue_capacity < 1 or not 1 <= batch_size <= 32:
            raise ValueError("invalid stage event writer queue or batch limit")
        if not 1 <= max_live_calls <= 8 or terminal_capacity < 1:
            raise ValueError("invalid stage event writer live or terminal limit")
        if max_active_tasks < 1:
            raise ValueError("stage event writer active task limit must be positive")
        # Keep enough bounded summary slots for every task already running.
        # This also keeps valid higher-concurrency worker configurations from
        # failing actor startup when the default queue is smaller than that cap.
        queue_capacity = max(queue_capacity, max_active_tasks)
        self._job_repo = job_repo
        self._live_sink = live_sink
        self._queue_capacity = queue_capacity
        self._summary_capacity_limit = 2 * queue_capacity - max_active_tasks
        self._max_batch_size = batch_size
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._write_timeout = write_timeout
        self._retry_delay = retry_delay
        self._retry_max_delay = max(retry_delay, retry_max_delay)
        self._next_retry_delay = min(retry_delay, self._retry_max_delay)
        self._shutdown_timeout = shutdown_timeout
        self._sample_interval = sample_interval
        self._live_timeout = live_timeout
        self._live_retry_delay = live_retry_delay
        self._max_live_calls = max_live_calls
        self._terminal_capacity = terminal_capacity
        self._terminal_ttl = terminal_ttl
        self._monotonic = monotonic

        self._events: deque[StageProgressEvent] = deque()
        self._summaries: OrderedDict[str, StageProgressSummary] = OrderedDict()
        self._summary_overflow_markers: OrderedDict[str, StageProgressSummary] = OrderedDict()
        self._inflight_event_task_ids: set[str] = set()
        self._inflight_summary_task_ids: set[str] = set()
        self._summary_capacity_ready = asyncio.Event()
        self._summary_capacity_ready.set()
        self._live_pending: OrderedDict[str, StageProgressWorkerSnapshot] = OrderedDict()
        self._trackers: dict[str, StageTracker] = {}
        self._terminal: OrderedDict[str, tuple[float, StageProgressWorkerSnapshot]] = OrderedDict()
        self._acknowledged: OrderedDict[tuple[str, int], int] = OrderedDict()
        self._event_ready = asyncio.Event()
        self._live_ready = asyncio.Event()
        self._closing = False
        self._writer_task: asyncio.Task[None] | None = None
        self._live_task: asyncio.Task[None] | None = None
        self._sampler_task: asyncio.Task[None] | None = None
        self._dropped_events = 0
        self._dropped_summaries = 0
        self._dropped_live_snapshots = 0
        self._write_failures = 0
        self._write_retries = 0
        self._live_failures = 0
        self._unacknowledged_terminal_evictions = 0
        self._pending_ack: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._known_incomplete: OrderedDict[str, None] = OrderedDict()
        self._single_event_timeouts: dict[UUID, int] = {}

    @property
    def queued_event_count(self) -> int:
        return len(self._events)

    @property
    def pending_summaries(self) -> dict[str, StageProgressSummary]:
        pending = dict(self._summaries)
        for task_id, marker in self._summary_overflow_markers.items():
            summary = pending.get(task_id)
            if summary is None or _summary_order(marker) > _summary_order(summary):
                pending[task_id] = marker
            elif not summary.known_loss:
                pending[task_id] = summary.model_copy(
                    update={
                        "known_loss": True,
                        "snapshot_revision": max(summary.snapshot_revision, marker.snapshot_revision),
                    }
                )
        return pending

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

    @property
    def write_failures(self) -> int:
        return self._write_failures

    @property
    def write_retries(self) -> int:
        return self._write_retries

    @property
    def dropped_summaries(self) -> int:
        return self._dropped_summaries

    @property
    def dropped_live_snapshots(self) -> int:
        return self._dropped_live_snapshots

    @property
    def live_failures(self) -> int:
        return self._live_failures

    @property
    def unacknowledged_terminal_evictions(self) -> int:
        return self._unacknowledged_terminal_evictions

    def register_tracker(self, tracker: StageTracker) -> None:
        self._trackers[tracker.task_id] = tracker
        self._ensure_started()

    async def wait_for_summary_capacity(self) -> None:
        """Pause new indexing work while bounded summary buffers approach capacity.

        Reserve one slot per active worker task. Existing tasks can finish and
        publish their final summaries without evicting a loss marker; new tasks
        resume once the writer drains enough summaries.
        """
        while True:
            if self._closing or not self._summary_capacity_saturated():
                return
            self._summary_capacity_ready.clear()
            if self._closing or not self._summary_capacity_saturated():
                continue
            await self._summary_capacity_ready.wait()

    def _summary_capacity_saturated(self) -> bool:
        pending_task_ids = set(self._summaries)
        pending_task_ids.update(self._summary_overflow_markers)
        pending_task_ids.update(event.task_id for event in self._events)
        pending_task_ids.update(self._inflight_event_task_ids)
        pending_task_ids.update(self._inflight_summary_task_ids)
        return len(pending_task_ids) >= self._summary_capacity_limit

    def _refresh_summary_capacity(self) -> None:
        if self._closing or not self._summary_capacity_saturated():
            self._summary_capacity_ready.set()
        else:
            self._summary_capacity_ready.clear()

    def snapshot_for_task(self, task_id: str) -> StageProgressWorkerSnapshot | None:
        tracker = self._trackers.get(task_id)
        if tracker is not None:
            return tracker.snapshot()
        terminal = self._terminal.get(task_id)
        if terminal is not None:
            return terminal[1]
        return None

    def publish(
        self,
        event: StageProgressEvent | None,
        summary: StageProgressSummary,
        event_tail: Sequence[StageProgressEvent],
    ) -> bool:
        """Queue a transition without waiting for I/O; coalesce its summary."""
        if self._closing or not self._ensure_started():
            if event is not None:
                self._dropped_events += 1
                record_stage_event_drop("shutdown")
            return False

        accepted = True
        if event is not None:
            if len(self._events) >= self._queue_capacity:
                self._dropped_events += 1
                record_stage_event_drop("queue_full")
                accepted = False
                summary = summary.model_copy(
                    update={"known_loss": True, "snapshot_revision": summary.snapshot_revision + 1}
                )
            else:
                self._events.append(event)
                set_stage_event_queue_size(len(self._events))
                self._event_ready.set()

        if summary.latest_sequence > self._acknowledged.get((summary.task_id, summary.attempt), 0):
            self._track_pending_ack((summary.task_id, summary.attempt))
        summary_accepted = self._put_latest_summary(summary)
        if not summary_accepted:
            accepted = False
            if not summary.known_loss:
                summary = summary.model_copy(
                    update={
                        "known_loss": True,
                        "snapshot_revision": min(summary.snapshot_revision + 1, _MAX_POSTGRES_BIGINT),
                    }
                )
        self._put_live_snapshot(summary, event_tail)
        self._refresh_metrics()
        return accepted

    def finish_task(self, tracker: StageTracker) -> None:
        """Retain a bounded terminal tail while its final write is pending."""
        tracker.finish_task()
        self._trackers.pop(tracker.task_id, None)
        snapshot = tracker.snapshot()
        self._put_live(snapshot)
        self._terminal[tracker.task_id] = (self._monotonic() + self._terminal_ttl, snapshot)
        self._terminal.move_to_end(tracker.task_id)
        if (
            snapshot.summary.expected_final_sequence is not None
            and self._acknowledged.get((tracker.task_id, snapshot.summary.attempt), 0)
            < snapshot.summary.expected_final_sequence
        ):
            self._track_pending_ack((tracker.task_id, snapshot.summary.attempt))
        self._expire_terminal()
        self._refresh_metrics()

    async def sample_active_once(self) -> None:
        """Publish one elapsed sample per active invocation without DB events."""
        if self._live_sink is None:
            return
        for tracker in tuple(self._trackers.values()):
            try:
                snapshot = tracker.sample_elapsed()
                if snapshot is not None:
                    await self._deliver_live(snapshot)
            except Exception:  # noqa: BLE001 - live observation is best-effort
                self._live_failures += 1

    async def close(self) -> None:
        """Attempt a bounded drain and then cancel any remaining background work."""
        if self._closing:
            return
        self._closing = True
        self._event_ready.set()
        self._live_ready.set()
        self._summary_capacity_ready.set()
        tasks = [task for task in (self._writer_task, self._live_task, self._sampler_task) if task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._shutdown_timeout)
            if pending:
                for task in pending:
                    task.cancel()
                _, pending = await asyncio.wait(pending, timeout=0.01)
                for task in pending:
                    task.add_done_callback(_consume_task_result)
        pending_events = tuple(self._events)
        pending_summaries = tuple(self.pending_summaries.values())
        self._events.clear()
        set_stage_event_queue_size(0)
        event_task_ids = {event.task_id for event in pending_events}
        if event_task_ids:
            self._drop_permanently_failed_tasks(
                event_task_ids,
                pending_events,
                pending_summaries,
                reason="shutdown",
            )
        summary_only = [summary for summary in pending_summaries if summary.task_id not in event_task_ids]
        if summary_only:
            self._drop_permanently_failed_tasks(
                {summary.task_id for summary in summary_only},
                [],
                summary_only,
                reason="shutdown",
            )
        await self._flush_shutdown_loss_markers()
        self._refresh_metrics()

    async def _flush_shutdown_loss_markers(self) -> None:
        markers = [summary for summary in self.pending_summaries.values() if summary.known_loss]
        if not markers:
            return
        try:
            await asyncio.wait_for(
                self._persist_shutdown_loss_markers(markers),
                timeout=min(_MIN_STAGE_WRITE_TIMEOUT, self._shutdown_timeout),
            )
        except Exception:  # noqa: BLE001 - shutdown persistence is best-effort and bounded
            logger.warning("Unable to persist all indexing stage history loss markers during shutdown")

    async def _persist_shutdown_loss_markers(self, markers: Sequence[StageProgressSummary]) -> None:
        for offset in range(0, len(markers), self._max_batch_size):
            batch = markers[offset : offset + self._max_batch_size]
            result = await self._job_repo.write_stage_progress(events=[], summaries=batch)
            failed_task_ids: set[str] = set()
            if isinstance(result, StageProgressWriteResult):
                failed_task_ids.update(result.permanent_failed_task_ids)
                failed_task_ids.update(result.missing_parent_task_ids)
            for summary in batch:
                if summary.task_id not in failed_task_ids:
                    self._summaries.pop(summary.task_id, None)
                    self._summary_overflow_markers.pop(summary.task_id, None)

    def _ensure_started(self) -> bool:
        if self._writer_task is not None:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._writer_task = loop.create_task(self._run_writer(), name="stage-event-writer")
        if self._live_sink is not None:
            self._live_task = loop.create_task(self._run_live_mailbox(), name="stage-live-mailbox")
            self._sampler_task = loop.create_task(self._run_sampler(), name="stage-elapsed-sampler")
        return True

    def _put_latest_summary(self, summary: StageProgressSummary) -> bool:
        previous = self._summaries.get(summary.task_id)
        overflow_marker = self._summary_overflow_markers.get(summary.task_id)
        if overflow_marker is not None and (
            previous is None or _summary_order(overflow_marker) > _summary_order(previous)
        ):
            previous = overflow_marker
        if previous is None or _summary_order(summary) >= _summary_order(previous):
            if previous is not None and previous.known_loss and not summary.known_loss:
                summary = summary.model_copy(
                    update={
                        "known_loss": True,
                        "snapshot_revision": max(summary.snapshot_revision, previous.snapshot_revision),
                    }
                )
            will_remove_overflow_marker = overflow_marker is not None and _summary_order(summary) >= _summary_order(
                overflow_marker
            )
            if (
                summary.task_id not in self._summaries
                and len(self._summaries) >= self._queue_capacity
                and len(self._summary_overflow_markers) >= self._queue_capacity
                and not will_remove_overflow_marker
            ):
                # Keep every queued loss marker. Production callers pass
                # through the admission gate, which reserves room for active
                # trackers; this defers only callers that bypass that gate.
                self._dropped_summaries += 1
                record_stage_event_drop("summary_overflow")
                self._mark_snapshot_loss(summary.task_id)
                self._remember_incomplete(summary.task_id)
                self._event_ready.set()
                self._refresh_summary_capacity()
                return False
            self._summary_overflow_markers.pop(summary.task_id, None)
            if summary.task_id not in self._summaries and len(self._summaries) >= self._queue_capacity:
                evicted_task_id, evicted_summary = self._summaries.popitem(last=False)
                self._dropped_summaries += 1
                record_stage_event_drop("summary_overflow")
                self._mark_snapshot_loss(evicted_task_id)
                self._remember_incomplete(evicted_task_id)
                if not self._queue_summary_overflow_marker(evicted_summary):
                    # Defensive fallback for callers that bypass the admission
                    # gate: restore the evicted summary and defer this update.
                    restored = evicted_summary.model_copy(
                        update={
                            "known_loss": True,
                            "snapshot_revision": min(
                                evicted_summary.snapshot_revision + int(not evicted_summary.known_loss),
                                _MAX_POSTGRES_BIGINT,
                            ),
                        }
                    )
                    self._summaries[evicted_task_id] = restored
                    self._summaries.move_to_end(evicted_task_id)
                    self._dropped_summaries += 1
                    record_stage_event_drop("summary_overflow")
                    self._mark_snapshot_loss(summary.task_id)
                    self._remember_incomplete(summary.task_id)
                    self._event_ready.set()
                    self._refresh_summary_capacity()
                    return False
            self._summaries[summary.task_id] = summary
            self._summaries.move_to_end(summary.task_id)
        self._event_ready.set()
        self._refresh_summary_capacity()
        return True

    def _queue_summary_overflow_marker(self, summary: StageProgressSummary) -> bool:
        task_id = summary.task_id
        candidates = [summary]
        previous = self._summary_overflow_markers.get(task_id)
        if previous is not None:
            candidates.append(previous)
        snapshot = self.snapshot_for_task(task_id)
        if snapshot is not None:
            candidates.append(snapshot.summary)
        latest = max(candidates, key=_summary_order)
        if not latest.known_loss:
            latest = latest.model_copy(
                update={
                    "known_loss": True,
                    "snapshot_revision": min(latest.snapshot_revision + 1, _MAX_POSTGRES_BIGINT),
                }
            )
        if previous is None and len(self._summary_overflow_markers) >= self._queue_capacity:
            return False
        self._summary_overflow_markers[task_id] = latest
        self._summary_overflow_markers.move_to_end(task_id)
        self._event_ready.set()
        self._refresh_summary_capacity()
        return True

    def _put_live_snapshot(
        self,
        summary: StageProgressSummary,
        event_tail: Sequence[StageProgressEvent],
    ) -> None:
        snapshot = StageProgressWorkerSnapshot(
            summary=summary,
            event_tail=list(event_tail[-32:]),
            active_invocation_id=summary.current_invocation_id,
        )
        self._put_live(snapshot)

    def _put_live(self, snapshot: StageProgressWorkerSnapshot) -> None:
        if self._live_sink is None:
            return
        task_id = snapshot.summary.task_id
        previous = self._live_pending.get(task_id)
        if previous is None or _summary_order(snapshot.summary) >= _summary_order(previous.summary):
            if previous is None and len(self._live_pending) >= self._queue_capacity:
                evicted_task_id, _ = self._live_pending.popitem(last=False)
                self._dropped_live_snapshots += 1
                record_stage_event_drop("live_snapshot_overflow")
                self._mark_snapshot_loss(evicted_task_id)
                self._remember_incomplete(evicted_task_id)
            self._live_pending[task_id] = snapshot
            self._live_pending.move_to_end(task_id)
        self._live_ready.set()

    def _expire_terminal(self) -> None:
        now = self._monotonic()
        for task_id, (expires_at, snapshot) in tuple(self._terminal.items()):
            if expires_at > now:
                continue
            self._record_terminal_eviction(task_id, snapshot)
            self._known_incomplete.pop(task_id, None)
            self._terminal.pop(task_id, None)
        while len(self._terminal) > self._terminal_capacity:
            task_id, (_, snapshot) = self._terminal.popitem(last=False)
            self._record_terminal_eviction(task_id, snapshot)
            self._known_incomplete.pop(task_id, None)

    def _record_terminal_eviction(self, task_id: str, snapshot: StageProgressWorkerSnapshot) -> None:
        expected = snapshot.summary.expected_final_sequence
        key = (task_id, snapshot.summary.attempt)
        persisted = self._acknowledged.get(key, 0)
        if expected is not None and (persisted < expected or snapshot.summary.known_loss):
            self._unacknowledged_terminal_evictions += 1
        self._pending_ack.pop(key, None)
        self._acknowledged.pop(key, None)

    async def _run_writer(self) -> None:
        while True:
            if not self._events and not self._summaries and not self._summary_overflow_markers:
                if self._closing:
                    return
                self._event_ready.clear()
                try:
                    await asyncio.wait_for(self._event_ready.wait(), timeout=self._flush_interval)
                except TimeoutError:
                    pass

            events = [self._events.popleft() for _ in range(min(self._batch_size, len(self._events)))]
            set_stage_event_queue_size(len(self._events))
            inflight_event_task_ids = {event.task_id for event in events}
            self._inflight_event_task_ids.update(inflight_event_task_ids)
            task_ids = {event.task_id for event in events}
            summaries_by_task: OrderedDict[str, StageProgressSummary] = OrderedDict()
            for queue in (self._summary_overflow_markers, self._summaries):
                if len(summaries_by_task) == self._batch_size:
                    break
                for task_id in tuple(queue):
                    if task_id in task_ids or len(summaries_by_task) < self._batch_size:
                        summary = queue.pop(task_id)
                        previous = summaries_by_task.get(task_id)
                        if previous is None or _summary_order(summary) >= _summary_order(previous):
                            if previous is not None and previous.known_loss and not summary.known_loss:
                                summary = summary.model_copy(update={"known_loss": True})
                            summaries_by_task[task_id] = summary
                        task_ids.add(task_id)
                    if len(summaries_by_task) == self._batch_size:
                        break
            summaries = list(summaries_by_task.values())
            self._inflight_summary_task_ids.update(summaries_by_task)
            self._refresh_summary_capacity()
            try:
                if events or summaries:
                    succeeded = await self._write_batch(events, summaries)
                    if not succeeded:
                        if self._closing:
                            return
                        delay = self._next_retry_delay
                        self._next_retry_delay = min(self._retry_max_delay, max(self._retry_delay, delay * 2))
                        await asyncio.sleep(delay)
            finally:
                self._inflight_event_task_ids.difference_update(inflight_event_task_ids)
                self._inflight_summary_task_ids.difference_update(summaries_by_task)
                self._refresh_summary_capacity()
            self._refresh_metrics()
            self._expire_terminal()
            if self._closing and not self._events and not self._summaries and not self._summary_overflow_markers:
                return

    async def _write_batch(
        self,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
    ) -> bool:
        # The repository performs work per event and per task summary. Keep each
        # write bounded, allow one second for a single task, and scale larger
        # batches in proportion to their size.
        write_timeout = max(_MIN_STAGE_WRITE_TIMEOUT, self._write_timeout * max(1, len(events), len(summaries)))
        for attempt in range(2):
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    self._job_repo.write_stage_progress(events=events, summaries=summaries),
                    timeout=write_timeout,
                )
            except asyncio.CancelledError:
                self._record_cancelled_batch_loss(events, summaries)
                raise
            except Exception as exc:  # noqa: BLE001 - repository failure cannot fail indexing
                duration = time.perf_counter() - started
                if _is_permanent_write_error(exc):
                    self._write_failures += 1
                    record_stage_event_write("failure", duration)
                    self._drop_permanently_failed_tasks(
                        {event.task_id for event in events} | {summary.task_id for summary in summaries},
                        events,
                        summaries,
                    )
                    self._refresh_metrics()
                    logger.warning(
                        "Indexing stage history batch was rejected permanently; marked its history incomplete",
                        sqlstate=getattr(exc, "sqlstate", None),
                    )
                    # The batch was dropped and accounted for; do not back off
                    # or requeue it as though the database might recover.
                    return True
                if attempt == 0:
                    self._write_retries += 1
                    record_stage_event_write("retry", duration)
                    try:
                        await asyncio.sleep(self._retry_delay)
                    except asyncio.CancelledError:
                        self._record_cancelled_batch_loss(events, summaries)
                        raise
                    continue
                self._write_failures += 1
                record_stage_event_write("failure", duration)
                if isinstance(exc, TimeoutError) and len(events) == 1:
                    event = events[0]
                    timeout_count = self._single_event_timeouts.get(event.event_id, 0) + 1
                    self._single_event_timeouts[event.event_id] = timeout_count
                    if timeout_count >= _MAX_SINGLE_EVENT_TIMEOUTS:
                        task_summaries = [summary for summary in summaries if summary.task_id == event.task_id]
                        for summary in summaries:
                            if summary.task_id != event.task_id:
                                self._put_latest_summary(summary)
                        self._drop_permanently_failed_tasks(
                            {event.task_id},
                            events,
                            task_summaries,
                            reason="write_timeout",
                        )
                        logger.warning(
                            "Indexing stage event exceeded its retry limit; marked its history incomplete",
                            task_id=event.task_id,
                        )
                        self._refresh_metrics()
                        return True
                else:
                    for event in events:
                        self._single_event_timeouts.pop(event.event_id, None)
                if self._closing:
                    self._drop_permanently_failed_tasks(
                        {event.task_id for event in events} | {summary.task_id for summary in summaries},
                        events,
                        summaries,
                        reason="shutdown",
                    )
                else:
                    self._write_retries += 1
                    record_stage_event_write("retry", duration)
                    if isinstance(exc, TimeoutError):
                        self._reduce_batch_size_after_timeout(events, summaries)
                    self._requeue_failed_batch(events, summaries)
                    logger.warning("Indexing stage history write failed; retained batch for retry")
                return False

            duration = time.perf_counter() - started
            record_stage_event_write("success", duration)
            for event in events:
                self._single_event_timeouts.pop(event.event_id, None)
            self._next_retry_delay = min(self._retry_delay, self._retry_max_delay)
            if self._batch_size < self._max_batch_size:
                self._batch_size += 1
            if isinstance(result, StageProgressWriteResult):
                for row in result.coverage:
                    key = (row.task_id, row.attempt)
                    self._acknowledged[key] = max(self._acknowledged.get(key, 0), row.persisted_through_sequence)
                    self._acknowledged.move_to_end(key)
                    while len(self._acknowledged) > self._queue_capacity + self._terminal_capacity:
                        self._acknowledged.popitem(last=False)
                    expected = next(
                        (
                            summary.expected_final_sequence
                            for summary in summaries
                            if summary.task_id == row.task_id and summary.attempt == row.attempt
                        ),
                        None,
                    )
                    latest = next(
                        (
                            summary.latest_sequence
                            for summary in summaries
                            if summary.task_id == row.task_id and summary.attempt == row.attempt
                        ),
                        row.persisted_through_sequence,
                    )
                    if row.persisted_through_sequence >= latest and (
                        expected is None or row.persisted_through_sequence >= expected
                    ):
                        self._pending_ack.pop(key, None)
                for task_id in result.conflicted_task_ids:
                    self._remember_incomplete(task_id)
                if result.permanent_failed_task_ids:
                    failed_task_ids = set(result.permanent_failed_task_ids)
                    self._write_failures += len(failed_task_ids)
                    record_stage_event_write("failure", duration)
                    self._drop_permanently_failed_tasks(failed_task_ids, events, summaries)
                    logger.warning(
                        "Indexing stage history writes were rejected for some tasks; marked their history incomplete",
                        task_count=len(failed_task_ids),
                    )
                for task_id in result.missing_parent_task_ids:
                    self._mark_snapshot_loss(task_id)
                    dropped_events = sum(event.task_id == task_id for event in events)
                    has_summary = any(summary.task_id == task_id for summary in summaries)
                    self._dropped_events += dropped_events
                    record_stage_event_drop("missing_parent", dropped_events or int(has_summary))
                    self._remember_incomplete(task_id)
            return True

        return False

    def _drop_permanently_failed_tasks(
        self,
        task_ids: set[str],
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
        *,
        reason: str = "write_failed",
    ) -> None:
        batch_task_ids = {event.task_id for event in events} | {summary.task_id for summary in summaries}
        for task_id in sorted(task_ids & batch_task_ids):
            task_events = [event for event in events if event.task_id == task_id]
            task_summaries = [summary for summary in summaries if summary.task_id == task_id]
            was_loss_marker = bool(task_summaries) and all(summary.known_loss for summary in task_summaries)
            self._drop_failed_batch(task_events, task_summaries, reason=reason)
            if not task_events and was_loss_marker and reason == "write_failed":
                continue

            candidates = list(task_summaries)
            pending = self._summaries.get(task_id)
            if pending is not None:
                candidates.append(pending)
            overflow_marker = self._summary_overflow_markers.get(task_id)
            if overflow_marker is not None:
                candidates.append(overflow_marker)
            snapshot = self.snapshot_for_task(task_id)
            if snapshot is not None:
                candidates.append(snapshot.summary)
            if not candidates:
                if not task_events:
                    continue
                # A finished task's terminal snapshot can expire while its
                # event backlog is still being drained. Keep a minimal durable
                # loss marker for that event attempt even after the snapshot
                # has been evicted. Zero counters keep the marker older than
                # any persisted projection, so the repository only applies the
                # known-loss bit instead of replacing the stage state.
                latest_event = max(task_events, key=lambda event: (event.attempt, event.sequence))
                self._put_latest_summary(
                    StageProgressSummary(
                        task_id=task_id,
                        attempt=latest_event.attempt,
                        phase=StageTaskPhase.PREPARING,
                        known_loss=True,
                    )
                )
                continue

            summary = max(candidates, key=_summary_order)
            if task_events and not task_summaries:
                next_revision = min(summary.snapshot_revision + 1, _MAX_POSTGRES_BIGINT)
                loss_summary = summary.model_copy(update={"known_loss": True, "snapshot_revision": next_revision})
            elif reason != "write_failed":
                loss_summary = summary.model_copy(
                    update={
                        "known_loss": True,
                        "snapshot_revision": min(
                            summary.snapshot_revision + int(not summary.known_loss),
                            _MAX_POSTGRES_BIGINT,
                        ),
                    }
                )
            else:
                # A summary-only batch may itself contain out-of-range or
                # schema-incompatible fields. Preserve its bounded counters,
                # but drop optional state so the durable loss marker can land.
                loss_summary = StageProgressSummary(
                    task_id=summary.task_id,
                    attempt=min(max(summary.attempt, 1), _MAX_POSTGRES_INTEGER),
                    latest_sequence=min(max(summary.latest_sequence, 0), _MAX_POSTGRES_BIGINT),
                    snapshot_revision=min(max(summary.snapshot_revision, 0), _MAX_POSTGRES_BIGINT),
                    phase=StageTaskPhase.PREPARING,
                    known_loss=True,
                )
            # The summary contains no failed event row. Persisting this marker
            # makes the dropped history visible to readers after a task ends.
            self._put_latest_summary(loss_summary)

    def _reduce_batch_size_after_timeout(
        self,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
    ) -> None:
        """Halve an over-deadline batch before retrying it ahead of newer events."""
        failed_batch_size = max(len(events), len(summaries))
        if failed_batch_size <= 1 or self._batch_size <= 1:
            return
        self._batch_size = max(1, min(self._batch_size // 2, failed_batch_size // 2))

    def _requeue_failed_batch(
        self,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
    ) -> None:
        # Events popped for an in-flight batch no longer occupy queue slots.
        # Put them ahead of newer transitions so retries preserve task order.
        queued_events = [*events, *self._events]
        retained_events = queued_events[: self._queue_capacity]
        dropped_events = queued_events[self._queue_capacity :]
        self._events = deque(retained_events)
        set_stage_event_queue_size(len(self._events))
        dropped_task_ids = {event.task_id for event in dropped_events}
        if dropped_events:
            self._dropped_events += len(dropped_events)
            record_stage_event_drop("queue_full", len(dropped_events))
            for task_id in dropped_task_ids:
                self._queue_loss_marker_for_events(
                    task_id,
                    [event for event in dropped_events if event.task_id == task_id],
                    summaries,
                )
            for event in dropped_events:
                self._single_event_timeouts.pop(event.event_id, None)

        for summary in summaries:
            if summary.task_id in dropped_task_ids and not summary.known_loss:
                summary = summary.model_copy(
                    update={
                        "known_loss": True,
                        "snapshot_revision": min(summary.snapshot_revision + 1, _MAX_POSTGRES_BIGINT),
                    }
                )
            self._put_latest_summary(summary)
        self._event_ready.set()

    def _queue_loss_marker_for_events(
        self,
        task_id: str,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary] = (),
    ) -> None:
        self._mark_snapshot_loss(task_id)
        latest_event = max(events, key=lambda event: (event.attempt, event.sequence))
        candidates = [summary for summary in summaries if summary.task_id == task_id]
        pending = self._summaries.get(task_id)
        if pending is not None:
            candidates.append(pending)
        overflow_marker = self._summary_overflow_markers.get(task_id)
        if overflow_marker is not None:
            candidates.append(overflow_marker)
        snapshot = self.snapshot_for_task(task_id)
        if snapshot is not None:
            candidates.append(snapshot.summary)
        summary = max(candidates, key=_summary_order) if candidates else None
        if summary is None or summary.attempt < latest_event.attempt:
            loss_marker = StageProgressSummary(
                task_id=task_id,
                attempt=latest_event.attempt,
                phase=StageTaskPhase.PREPARING,
                known_loss=True,
            )
        elif summary.known_loss:
            loss_marker = summary
        else:
            loss_marker = summary.model_copy(
                update={
                    "known_loss": True,
                    "snapshot_revision": min(summary.snapshot_revision + 1, _MAX_POSTGRES_BIGINT),
                }
            )
        self._put_latest_summary(loss_marker)

    def _drop_failed_batch(
        self,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
        *,
        reason: str,
    ) -> None:
        if events:
            self._dropped_events += len(events)
            for event in events:
                self._single_event_timeouts.pop(event.event_id, None)
        record_stage_event_drop(reason, len(events) or len(summaries))
        affected_attempts = {(event.task_id, event.attempt) for event in events}
        affected_attempts.update((summary.task_id, summary.attempt) for summary in summaries)
        for key in affected_attempts:
            self._pending_ack.pop(key, None)
        for task_id in {event.task_id for event in events} | {summary.task_id for summary in summaries}:
            self._mark_snapshot_loss(task_id)
            self._remember_incomplete(task_id)

    def _record_cancelled_batch_loss(
        self,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
    ) -> None:
        self._drop_permanently_failed_tasks(
            {event.task_id for event in events} | {summary.task_id for summary in summaries},
            events,
            summaries,
            reason="shutdown",
        )

    def _mark_snapshot_loss(self, task_id: str) -> None:
        self._remember_incomplete(task_id)
        for summaries in (self._summaries, self._summary_overflow_markers):
            summary = summaries.get(task_id)
            if summary is not None and not summary.known_loss:
                summaries[task_id] = summary.model_copy(
                    update={
                        "known_loss": True,
                        "snapshot_revision": min(summary.snapshot_revision + 1, _MAX_POSTGRES_BIGINT),
                    }
                )
        for tracker in (self._trackers.get(task_id),):
            if tracker is not None:
                tracker.mark_loss()
        terminal = self._terminal.get(task_id)
        if terminal is not None:
            expires_at, snapshot = terminal
            if not snapshot.summary.known_loss:
                summary = snapshot.summary.model_copy(
                    update={
                        "known_loss": True,
                        "snapshot_revision": min(snapshot.summary.snapshot_revision + 1, _MAX_POSTGRES_BIGINT),
                    }
                )
                snapshot = snapshot.model_copy(update={"summary": summary})
                self._terminal[task_id] = (expires_at, snapshot)
                self._put_live(snapshot)

    async def _run_live_mailbox(self) -> None:
        semaphore = asyncio.Semaphore(self._max_live_calls)
        while True:
            if not self._live_pending:
                if self._closing:
                    return
                self._live_ready.clear()
                try:
                    await asyncio.wait_for(self._live_ready.wait(), timeout=self._flush_interval)
                except TimeoutError:
                    pass
            batch = []
            while self._live_pending and len(batch) < self._max_live_calls:
                _, snapshot = self._live_pending.popitem(last=False)
                batch.append(snapshot)
            if batch:
                await asyncio.gather(*(self._deliver_live(snapshot, semaphore=semaphore) for snapshot in batch))
            if self._closing and not self._live_pending:
                return

    async def _deliver_live(
        self,
        snapshot: StageProgressWorkerSnapshot,
        *,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        if self._live_sink is None:
            return
        gate = semaphore or asyncio.Semaphore(1)
        async with gate:
            for attempt in range(2):
                try:
                    result = self._live_sink(snapshot)
                    if inspect.isawaitable(result):
                        result = await asyncio.wait_for(result, timeout=self._live_timeout)
                    if result is False:
                        raise RuntimeError("live state rejected stage snapshot")
                    return
                except Exception:  # noqa: BLE001 - TSM trouble must not affect indexing
                    if attempt == 0:
                        await asyncio.sleep(self._live_retry_delay)
                        continue
                    self._live_failures += 1
                    record_stage_live_snapshot_failure()

    async def _run_sampler(self) -> None:
        while not self._closing:
            await asyncio.sleep(self._sample_interval)
            if self._closing:
                return
            for tracker in tuple(self._trackers.values()):
                try:
                    snapshot = tracker.sample_elapsed()
                    if snapshot is not None:
                        self._put_live(snapshot)
                except Exception:  # noqa: BLE001 - elapsed sampling is best-effort
                    self._live_failures += 1
                    record_stage_live_snapshot_failure()

    def _refresh_metrics(self) -> None:
        self._refresh_summary_capacity()
        pending = set(self._pending_ack)
        pending.update((event.task_id, event.attempt) for event in self._events)
        pending.update((summary.task_id, summary.attempt) for summary in self._summaries.values())
        pending.update((summary.task_id, summary.attempt) for summary in self._summary_overflow_markers.values())
        set_stage_history_counts(pending=len(pending), incomplete=len(self._known_incomplete))

    def _remember_incomplete(self, task_id: str) -> None:
        self._known_incomplete[task_id] = None
        self._known_incomplete.move_to_end(task_id)
        while len(self._known_incomplete) > self._queue_capacity:
            self._known_incomplete.popitem(last=False)

    def _track_pending_ack(self, key: tuple[str, int]) -> None:
        self._pending_ack[key] = None
        self._pending_ack.move_to_end(key)
        while len(self._pending_ack) > self._queue_capacity + self._terminal_capacity:
            (task_id, _), _ = self._pending_ack.popitem(last=False)
            self._remember_incomplete(task_id)
            record_stage_event_drop("summary_overflow")


def _summary_order(summary: StageProgressSummary) -> tuple[int, int, int]:
    return summary.attempt, summary.latest_sequence, summary.snapshot_revision


def _consume_task_result(task: asyncio.Task[None]) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return
