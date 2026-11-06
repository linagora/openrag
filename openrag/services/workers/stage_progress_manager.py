"""Short-lived live stage snapshots independent of TaskStateManager generations."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from time import monotonic

import ray
from core.models.stage_progress import StageProgressWorkerSnapshot
from services.workers.task_state import WORKER_LEASE_RENEW_INTERVAL_SECONDS

TASK_STAGE_PROGRESS_MANAGER_NAME = "TaskStageProgressManager-v1"
_LIVE_SNAPSHOT_TTL_SECONDS = 2 * WORKER_LEASE_RENEW_INTERVAL_SECONDS
_MAX_LIVE_SNAPSHOTS = 10_000


@ray.remote(concurrency_groups={"set": 1000, "get": 1000})
class TaskStageProgressManager:
    """Keep fresh worker snapshots when an older task-state actor is reused."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _LIVE_SNAPSHOT_TTL_SECONDS,
        max_snapshots: int = _MAX_LIVE_SNAPSHOTS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("live stage snapshot TTL must be positive")
        if max_snapshots < 1:
            raise ValueError("live stage snapshot capacity must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_snapshots = max_snapshots
        self._snapshots: OrderedDict[str, tuple[StageProgressWorkerSnapshot, float]] = OrderedDict()
        self._lock = Lock()

    @ray.method(concurrency_group="set")
    async def set_snapshot(self, task_id: str, snapshot: StageProgressWorkerSnapshot | None) -> bool:
        """Store a compact snapshot; callers refresh it on worker lease renewals."""
        if not isinstance(task_id, str) or not task_id:
            return False
        with self._lock:
            self._expire_locked(monotonic())
            if snapshot is None:
                self._snapshots.pop(task_id, None)
                return True
            if not isinstance(snapshot, StageProgressWorkerSnapshot):
                try:
                    snapshot = StageProgressWorkerSnapshot.model_validate(snapshot)
                except Exception:
                    return False
            if (
                snapshot.summary.task_id != task_id
                or snapshot.active_invocation_id != snapshot.summary.current_invocation_id
            ):
                return False
            current = self._snapshots.get(task_id)
            if current is not None and _snapshot_order(snapshot) < _snapshot_order(current[0]):
                return False
            self._snapshots[task_id] = (
                snapshot.model_copy(update={"event_tail": []}),
                monotonic() + self._ttl_seconds,
            )
            self._snapshots.move_to_end(task_id)
            while len(self._snapshots) > self._max_snapshots:
                self._snapshots.popitem(last=False)
            return True

    @ray.method(concurrency_group="set")
    async def remove_snapshot(self, task_id: str) -> None:
        with self._lock:
            self._snapshots.pop(task_id, None)

    @ray.method(concurrency_group="get")
    async def get_stage_progress_snapshots(self, task_ids: list[str]) -> dict[str, StageProgressWorkerSnapshot]:
        """Return only snapshots refreshed within the worker lease window."""
        with self._lock:
            self._expire_locked(monotonic())
            return {task_id: self._snapshots[task_id][0] for task_id in task_ids if task_id in self._snapshots}

    def _expire_locked(self, now: float) -> None:
        for task_id, (_, expires_at) in list(self._snapshots.items()):
            if expires_at <= now:
                self._snapshots.pop(task_id, None)


def _snapshot_order(snapshot: StageProgressWorkerSnapshot) -> tuple[int, int]:
    return (snapshot.summary.attempt, snapshot.summary.snapshot_revision)


__all__ = ["TASK_STAGE_PROGRESS_MANAGER_NAME", "TaskStageProgressManager"]
