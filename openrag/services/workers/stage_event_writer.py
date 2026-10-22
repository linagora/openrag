"""Bounded best-effort delivery for worker-observed indexing stage progress."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from typing import Any

from core.models.stage_progress import (
    StageProgressEvent,
    StageProgressSummary,
    StageProgressWorkerSnapshot,
    StageProgressWriteResult,
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


class StageEventWriter:
    """Own one bounded event queue and coalesced live mailbox per worker actor.

    ``publish`` is synchronous and uses only ``put_nowait``-style operations.
    PostgreSQL and TaskStateManager calls happen in background tasks with
    explicit deadlines and one retry.
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
        shutdown_timeout: float = 0.25,
        sample_interval: float = 1.0,
        live_timeout: float = 0.075,
        live_retry_delay: float = 0.025,
        max_live_calls: int = 8,
        terminal_capacity: int = 32,
        terminal_ttl: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if queue_capacity < 1 or not 1 <= batch_size <= 32:
            raise ValueError("invalid stage event writer queue or batch limit")
        if not 1 <= max_live_calls <= 8 or terminal_capacity < 1:
            raise ValueError("invalid stage event writer live or terminal limit")
        self._job_repo = job_repo
        self._live_sink = live_sink
        self._queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._write_timeout = write_timeout
        self._retry_delay = retry_delay
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

    @property
    def queued_event_count(self) -> int:
        return len(self._events)

    @property
    def pending_summaries(self) -> dict[str, StageProgressSummary]:
        return dict(self._summaries)

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
        self._put_latest_summary(summary)
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
        tasks = [task for task in (self._writer_task, self._live_task, self._sampler_task) if task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._shutdown_timeout)
            if pending:
                for task in pending:
                    task.cancel()
                _, pending = await asyncio.wait(pending, timeout=0.01)
                for task in pending:
                    task.add_done_callback(_consume_task_result)
        if self._events:
            self._dropped_events += len(self._events)
            record_stage_event_drop("shutdown", len(self._events))
            self._events.clear()
            set_stage_event_queue_size(0)
        if self._summaries:
            for summary in self._summaries.values():
                self._mark_snapshot_loss(summary.task_id)
            self._summaries.clear()
        self._refresh_metrics()

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

    def _put_latest_summary(self, summary: StageProgressSummary) -> None:
        previous = self._summaries.get(summary.task_id)
        if previous is None or _summary_order(summary) >= _summary_order(previous):
            if previous is None and len(self._summaries) >= self._queue_capacity:
                evicted_task_id, _ = self._summaries.popitem(last=False)
                self._dropped_summaries += 1
                record_stage_event_drop("summary_overflow")
                self._mark_snapshot_loss(evicted_task_id)
                self._remember_incomplete(evicted_task_id)
            self._summaries[summary.task_id] = summary
            self._summaries.move_to_end(summary.task_id)
        self._event_ready.set()

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
                record_stage_live_snapshot_failure()
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
            if not self._events and not self._summaries:
                if self._closing:
                    return
                self._event_ready.clear()
                try:
                    await asyncio.wait_for(self._event_ready.wait(), timeout=self._flush_interval)
                except TimeoutError:
                    pass

            events = [self._events.popleft() for _ in range(min(self._batch_size, len(self._events)))]
            set_stage_event_queue_size(len(self._events))
            task_ids = {event.task_id for event in events}
            summaries: list[StageProgressSummary] = []
            for task_id in tuple(self._summaries):
                if task_id in task_ids or len(summaries) < self._batch_size:
                    summaries.append(self._summaries.pop(task_id))
                    task_ids.add(task_id)
                if len(summaries) == self._batch_size:
                    break
            if events or summaries:
                await self._write_batch(events, summaries)
            self._refresh_metrics()
            self._expire_terminal()
            if self._closing and not self._events and not self._summaries:
                return

    async def _write_batch(
        self,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
    ) -> None:
        for attempt in range(2):
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    self._job_repo.write_stage_progress(events=events, summaries=summaries),
                    timeout=self._write_timeout,
                )
            except asyncio.CancelledError:
                self._dropped_events += len(events)
                record_stage_event_drop("shutdown", len(events))
                for summary in summaries:
                    self._mark_snapshot_loss(summary.task_id)
                raise
            except Exception:  # noqa: BLE001 - repository failure cannot fail indexing
                duration = time.perf_counter() - started
                if attempt == 0:
                    self._write_retries += 1
                    record_stage_event_write("retry", duration)
                    await asyncio.sleep(self._retry_delay)
                    continue
                self._write_failures += 1
                self._dropped_events += len(events)
                record_stage_event_write("failure", duration)
                record_stage_event_drop("write_failed", len(events))
                for summary in summaries:
                    self._mark_snapshot_loss(summary.task_id)
                    self._remember_incomplete(summary.task_id)
                logger.warning("Indexing stage history write failed after bounded retry")
                return

            record_stage_event_write("success", time.perf_counter() - started)
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
                for task_id in result.missing_parent_task_ids:
                    self._mark_snapshot_loss(task_id)
                    self._dropped_events += sum(event.task_id == task_id for event in events)
                    record_stage_event_drop("missing_parent", sum(event.task_id == task_id for event in events))
                    self._remember_incomplete(task_id)
            return

    def _mark_snapshot_loss(self, task_id: str) -> None:
        self._remember_incomplete(task_id)
        summary = self._summaries.get(task_id)
        if summary is not None and not summary.known_loss:
            self._summaries[task_id] = summary.model_copy(
                update={"known_loss": True, "snapshot_revision": summary.snapshot_revision + 1}
            )
        for tracker in (self._trackers.get(task_id),):
            if tracker is not None:
                tracker.mark_loss()
        terminal = self._terminal.get(task_id)
        if terminal is not None:
            expires_at, snapshot = terminal
            if not snapshot.summary.known_loss:
                summary = snapshot.summary.model_copy(
                    update={"known_loss": True, "snapshot_revision": snapshot.summary.snapshot_revision + 1}
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
        pending = set(self._pending_ack)
        pending.update((event.task_id, event.attempt) for event in self._events)
        pending.update((summary.task_id, summary.attempt) for summary in self._summaries.values())
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
