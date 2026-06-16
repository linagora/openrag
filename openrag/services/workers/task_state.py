from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import ray

try:
    from core.config import load_config as _load_config

    _cfg = _load_config()
    _POOL_SIZE: int = _cfg.ray.pool_size
    _MAX_TASKS_PER_WORKER: int = _cfg.ray.max_tasks_per_worker
except (ImportError, AttributeError) as _cfg_err:
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "Could not load ray config for TaskStateManager pool info: %s — using defaults", _cfg_err
    )
    _POOL_SIZE = 1
    _MAX_TASKS_PER_WORKER = 1


@dataclass
class TaskInfo:
    state: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    object_ref: ray.ObjectRef | None = None
    current_stage: str | None = None
    current_stage_started_at: float | None = None
    failed_stage: str | None = None
    stage_durations: dict[str, float] = field(default_factory=dict)
    stage_history: list[dict[str, Any]] = field(default_factory=list)


def _stage_duration_key(stage: str) -> str:
    return f"{stage.lower()}_seconds"


def _close_current_stage(info: TaskInfo, ended_at: float) -> None:
    if info.current_stage is None or info.current_stage_started_at is None:
        info.current_stage = None
        info.current_stage_started_at = None
        return

    duration = max(0.0, ended_at - info.current_stage_started_at)
    key = _stage_duration_key(info.current_stage)
    info.stage_durations[key] = round(info.stage_durations.get(key, 0.0) + duration, 3)
    info.stage_history.append(
        {
            "stage": info.current_stage,
            "started_at": info.current_stage_started_at,
            "ended_at": ended_at,
            "duration_seconds": round(duration, 3),
        }
    )
    info.current_stage = None
    info.current_stage_started_at = None


def _info_snapshot(info: TaskInfo) -> dict[str, Any]:
    return {
        "state": info.state,
        "error": info.error,
        "details": info.details,
        "current_stage": info.current_stage,
        "failed_stage": info.failed_stage,
        "stage_durations": dict(info.stage_durations),
        "stage_history": list(info.stage_history),
    }


@ray.remote(concurrency_groups={"set": 1000, "get": 1000, "queue_info": 1000})
class TaskStateManager:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskInfo] = {}
        self.user_index: dict[int, set[str]] = {}
        self.lock = asyncio.Lock()

    async def _ensure_task(self, task_id: str) -> TaskInfo:
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskInfo()
        return self.tasks[task_id]

    @ray.method(concurrency_group="set")
    async def set_state(self, task_id: str, state: str) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            if state in {"COMPLETED", "FAILED", "CANCELLED"}:
                _close_current_stage(info, time.time())
            info.state = state

    @ray.method(concurrency_group="set")
    async def set_error(self, task_id: str, tb_str: str) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.error = tb_str

    @ray.method(concurrency_group="set")
    async def set_failed_if_not_cancelled(self, task_id: str, tb_str: str) -> bool:
        """Atomically set state to FAILED and record the traceback, unless already CANCELLED."""
        async with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state == "CANCELLED":
                return False
            info.failed_stage = info.current_stage
            _close_current_stage(info, time.time())
            info.state = "FAILED"
            info.error = tb_str
            return True

    @ray.method(concurrency_group="set")
    async def start_stage(self, task_id: str, stage: str) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            if info.current_stage == stage and info.current_stage_started_at is not None:
                return
            now = time.time()
            _close_current_stage(info, now)
            info.current_stage = stage
            info.current_stage_started_at = now

    @ray.method(concurrency_group="set")
    async def finish_current_stage(self, task_id: str) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            _close_current_stage(info, time.time())

    @ray.method(concurrency_group="set")
    async def set_details(
        self,
        task_id: str,
        *,
        file_id: str,
        partition: int,
        metadata: dict,
        user_id: int,
    ) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.details = {
                "file_id": file_id,
                "partition": partition,
                "metadata": metadata,
                "user_id": user_id,
            }
            self.user_index.setdefault(user_id, set()).add(task_id)

    @ray.method(concurrency_group="set")
    async def set_object_ref(self, task_id: str, object_ref: ray.ObjectRef) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.object_ref = object_ref

    @ray.method(concurrency_group="get")
    async def get_state(self, task_id: str) -> str | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.state if info else None

    @ray.method(concurrency_group="get")
    async def get_error(self, task_id: str) -> str | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.error if info else None

    @ray.method(concurrency_group="get")
    async def get_details(self, task_id: str) -> dict | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.details if info else None

    @ray.method(concurrency_group="get")
    async def get_object_ref(self, task_id: str) -> ray.ObjectRef | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.object_ref if info else None

    @ray.method(concurrency_group="get")
    async def get_info(self, task_id: str) -> dict | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return _info_snapshot(info) if info else None

    @ray.method(concurrency_group="queue_info")
    async def get_all_states(self) -> dict[str, str | None]:
        async with self.lock:
            return {tid: info.state for tid, info in self.tasks.items()}

    @ray.method(concurrency_group="queue_info")
    async def get_all_info(self) -> dict[str, dict]:
        async with self.lock:
            return {task_id: _info_snapshot(info) for task_id, info in self.tasks.items()}

    @ray.method(concurrency_group="queue_info")
    async def get_all_user_info(self, user_id: int) -> dict[str, dict]:
        async with self.lock:
            task_ids = self.user_index.get(user_id, set())
            return {tid: _info_snapshot(self.tasks[tid]) for tid in task_ids if tid in self.tasks}

    @ray.method(concurrency_group="queue_info")
    async def get_pool_info(self) -> dict[str, int]:
        return {
            "pool_size": _POOL_SIZE,
            "max_tasks_per_worker": _MAX_TASKS_PER_WORKER,
            "total_capacity": _POOL_SIZE * _MAX_TASKS_PER_WORKER,
        }

    @ray.method(concurrency_group="queue_info")
    async def get_user_pending_task_count(self, user_id: int) -> int:
        async with self.lock:
            task_ids = self.user_index.get(user_id, set())
            pending_states = {"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"}
            return sum(1 for tid in task_ids if (info := self.tasks.get(tid)) and info.state in pending_states)


__all__ = ["TaskInfo", "TaskStateManager"]
