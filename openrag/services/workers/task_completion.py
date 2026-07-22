from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import ray
from core.models.catalog import TASK_FINISHED_AT_METADATA_KEY, TERMINAL_TASK_STATES

_TERMINAL_STATES = frozenset(state.value for state in TERMINAL_TASK_STATES)
_REFLESS_RECOVERY_POLL_SECONDS = 5.0


class TaskCompletionTracker:
    """Keep indexing completion observation alive outside API processes."""

    def __init__(self, namespace: str = "openrag") -> None:
        from core.utils.logging import get_logger

        self._namespace = namespace
        self._logger = get_logger()
        self._tracked_task_ids: set[str] = set()
        self._recovery_lock = asyncio.Lock()

    async def track(self, task_id: str, object_ref: dict[str, Any]) -> None:
        ref = object_ref.get("ref")
        if ref is None:
            raise ValueError(f"Missing worker reference for task {task_id}")
        if task_id in self._tracked_task_ids:
            return
        self._tracked_task_ids.add(task_id)
        try:
            await asyncio.gather(ref, return_exceptions=True)
            await self._record_finished_at(task_id)
        except Exception as exc:
            self._logger.warning(
                "Failed to record indexing task completion time",
                task_id=task_id,
                error=str(exc),
            )
        finally:
            self._tracked_task_ids.discard(task_id)

    async def recover(self) -> None:
        """Recover watches missed during API or tracker restarts."""
        async with self._recovery_lock:
            try:
                task_state_manager = self._task_state_manager()
                tracker = ray.get_actor("TaskCompletionTracker", namespace=self._namespace)
                all_info = await task_state_manager.get_all_info.remote()
                for task_id, info in all_info.items():
                    if _has_finished_at(info.get("details")):
                        continue
                    if info.get("state") in _TERMINAL_STATES:
                        await self._record_finished_at(task_id)
                        continue
                    object_ref = await task_state_manager.get_object_ref.remote(task_id)
                    if isinstance(object_ref, dict) and object_ref.get("ref") is not None:
                        tracker.track.remote(task_id, object_ref)
                    else:
                        tracker.recover_refless.remote(task_id)
            except Exception as exc:
                self._logger.warning("Failed to recover indexing completion tracking", error=str(exc))

    async def recover_refless(self, task_id: str, poll_interval: float = _REFLESS_RECOVERY_POLL_SECONDS) -> None:
        """Watch a recovered active task whose ObjectRef has not been stored yet."""
        if task_id in self._tracked_task_ids:
            return
        self._tracked_task_ids.add(task_id)
        try:
            while True:
                task_state_manager = self._task_state_manager()
                details = await task_state_manager.get_details.remote(task_id)
                if _has_finished_at(details):
                    return

                state = await task_state_manager.get_state.remote(task_id)
                if state in _TERMINAL_STATES:
                    await self._record_finished_at(task_id)
                    return

                object_ref = await task_state_manager.get_object_ref.remote(task_id)
                ref = object_ref.get("ref") if isinstance(object_ref, dict) else None
                if ref is not None:
                    await asyncio.gather(ref, return_exceptions=True)
                    await self._record_finished_at(task_id)
                    return

                await asyncio.sleep(poll_interval)
        except Exception as exc:
            self._logger.warning(
                "Failed to recover ref-less indexing task completion tracking",
                task_id=task_id,
                error=str(exc),
            )
        finally:
            self._tracked_task_ids.discard(task_id)

    async def _record_finished_at(self, task_id: str) -> None:
        task_state_manager = self._task_state_manager()
        details = await task_state_manager.get_details.remote(task_id)
        if not isinstance(details, dict) or _has_finished_at(details):
            return

        metadata = details.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata[TASK_FINISHED_AT_METADATA_KEY] = _utc_now_iso()
        await task_state_manager.set_details.remote(
            task_id,
            file_id=details.get("file_id"),
            partition=details.get("partition"),
            metadata=metadata,
            user_id=details.get("user_id"),
        )

    def _task_state_manager(self) -> Any:
        return ray.get_actor("TaskStateManager", namespace=self._namespace)


TaskCompletionTrackerActor = ray.remote(max_restarts=-1, max_task_retries=-1, max_concurrency=1000)(
    TaskCompletionTracker
)


def _has_finished_at(details: Any) -> bool:
    if not isinstance(details, dict):
        return False
    metadata = details.get("metadata")
    return isinstance(metadata, dict) and TASK_FINISHED_AT_METADATA_KEY in metadata


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["TaskCompletionTracker", "TaskCompletionTrackerActor"]
