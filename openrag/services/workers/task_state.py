from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import ray
from core.models.catalog import TERMINAL_TASK_STATES, DocumentStatus

ACTIVE_INDEXING_STATES = frozenset({"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"})
TERMINAL_INDEXING_STATES = frozenset({"COMPLETED", "FAILED"})
PENDING_TASK_DETAILS = "__openrag_pending_task_details__"

try:
    from core.config import load_config as _load_config

    _cfg = _load_config()
    _POOL_SIZE: int = _cfg.ray.indexer.pool_size
    _MAX_TASKS_PER_WORKER: int = _cfg.ray.indexer.max_tasks_per_worker
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


@ray.remote(concurrency_groups={"set": 1000, "get": 1000, "queue_info": 1000})
class TaskStateManager:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskInfo] = {}
        self.user_index: dict[int | None, set[str]] = {}
        self.file_delete_fences: dict[tuple[str, str], int] = {}
        self.lock = asyncio.Lock()

    async def _ensure_task(self, task_id: str) -> TaskInfo:
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskInfo()
        return self.tasks[task_id]

    def _record_details(
        self,
        task_id: str,
        info: TaskInfo,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
    ) -> None:
        info.details = {
            "file_id": file_id,
            "partition": partition,
            "metadata": metadata,
            "user_id": user_id,
        }
        self.user_index.setdefault(user_id, set()).add(task_id)

    def _file_delete_fenced(self, *, partition: str | None, file_id: str | None) -> bool:
        if partition is None or file_id is None:
            return False
        return self.file_delete_fences.get((partition, file_id), 0) > 0

    @ray.method(concurrency_group="set")
    async def begin_file_delete(self, *, partition: str, file_id: str) -> None:
        async with self.lock:
            key = (partition, file_id)
            self.file_delete_fences[key] = self.file_delete_fences.get(key, 0) + 1

    @ray.method(concurrency_group="set")
    async def end_file_delete(self, *, partition: str, file_id: str) -> None:
        async with self.lock:
            key = (partition, file_id)
            remaining = self.file_delete_fences.get(key, 0) - 1
            if remaining > 0:
                self.file_delete_fences[key] = remaining
            else:
                self.file_delete_fences.pop(key, None)

    @ray.method(concurrency_group="set")
    async def set_state(self, task_id: str, state: str) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            if info.state == DocumentStatus.CANCELLED and state != DocumentStatus.CANCELLED:
                return
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
            info.state = "FAILED"
            info.error = tb_str
            return True

    @ray.method(concurrency_group="set")
    async def set_cancelled_if_active(self, task_id: str) -> bool:
        async with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state in TERMINAL_TASK_STATES:
                return False
            info.state = "CANCELLED"
            return True

    @ray.method(concurrency_group="set")
    async def set_details(
        self,
        task_id: str,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
    ) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            self._record_details(
                task_id,
                info,
                file_id=file_id,
                partition=partition,
                metadata=metadata,
                user_id=user_id,
            )

    @ray.method(concurrency_group="set")
    async def set_queued_details(
        self,
        task_id: str,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
    ) -> bool:
        async with self.lock:
            info = await self._ensure_task(task_id)
            if info.state == DocumentStatus.CANCELLED:
                return False
            self._record_details(
                task_id,
                info,
                file_id=file_id,
                partition=partition,
                metadata=metadata,
                user_id=user_id,
            )
            if self._file_delete_fenced(partition=partition, file_id=file_id):
                info.state = "CANCELLED"
                return False
            info.state = "QUEUED"
            return True

    @ray.method(concurrency_group="set")
    async def set_object_ref(self, task_id: str, object_ref: ray.ObjectRef) -> bool:
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.object_ref = object_ref
            details = info.details or {}
            if self._file_delete_fenced(partition=details.get("partition"), file_id=details.get("file_id")):
                info.state = "CANCELLED"
                return False
            return info.state in ACTIVE_INDEXING_STATES or info.state in TERMINAL_INDEXING_STATES

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
    async def get_matching_active_task_refs(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        async with self.lock:
            return self._matching_active_task_refs_locked(partition=partition, file_id=file_id)

    @ray.method(concurrency_group="get")
    async def get_matching_active_task_refs_v2(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        async with self.lock:
            return self._matching_active_task_refs_locked(partition=partition, file_id=file_id)

    def _matching_active_task_refs_locked(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        matches = {}
        for task_id, info in self.tasks.items():
            if info.state not in ACTIVE_INDEXING_STATES:
                continue
            details = info.details or {}
            if not details:
                matches[task_id] = PENDING_TASK_DETAILS
                continue
            if details.get("partition") != partition:
                continue
            if file_id is not None and details.get("file_id") != file_id:
                continue
            matches[task_id] = info.object_ref
        return matches

    @ray.method(concurrency_group="queue_info")
    async def get_all_states(self) -> dict[str, str | None]:
        async with self.lock:
            return {tid: info.state for tid, info in self.tasks.items()}

    @ray.method(concurrency_group="queue_info")
    async def get_all_info(self) -> dict[str, dict]:
        async with self.lock:
            return {
                task_id: {
                    "state": info.state,
                    "error": info.error,
                    "details": info.details,
                }
                for task_id, info in self.tasks.items()
            }

    @ray.method(concurrency_group="queue_info")
    async def get_all_user_info(self, user_id: int) -> dict[str, dict]:
        async with self.lock:
            task_ids = self.user_index.get(user_id, set())
            return {
                tid: {
                    "state": self.tasks[tid].state,
                    "error": self.tasks[tid].error,
                    "details": self.tasks[tid].details,
                }
                for tid in task_ids
                if tid in self.tasks
            }

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
            return sum(1 for tid in task_ids if (info := self.tasks.get(tid)) and info.state in ACTIVE_INDEXING_STATES)


__all__ = [
    "ACTIVE_INDEXING_STATES",
    "PENDING_TASK_DETAILS",
    "TERMINAL_INDEXING_STATES",
    "TaskInfo",
    "TaskStateManager",
]
