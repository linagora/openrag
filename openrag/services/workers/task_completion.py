from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import ray
from core.models.catalog import TASK_FINISHED_AT_METADATA_KEY, TERMINAL_TASK_STATES
from services.workers.ray_utils import call_ray_actor_method_with_timeout

_TERMINAL_STATES = frozenset(state.value for state in TERMINAL_TASK_STATES)
_REFLESS_RECOVERY_POLL_SECONDS = 5.0
_TASK_STATE_CALL_TIMEOUT_SECONDS = 30.0


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
            await self._finish_cancellation(task_id)
            self._tracked_task_ids.discard(task_id)

    async def recover(self) -> None:
        """Recover watches missed during API or tracker restarts."""
        async with self._recovery_lock:
            try:
                task_state_manager = self._task_state_manager()
                tracker = ray.get_actor("TaskCompletionTracker", namespace=self._namespace)
                all_info = await self._call_task_state(
                    task_state_manager.get_all_info.remote,
                    "get_all_info_for_completion_recovery",
                )
                for task_id, info in all_info.items():
                    state = info.get("state")
                    if state == "CANCELLED":
                        object_ref = await self._call_task_state(
                            lambda task_id=task_id: task_state_manager.get_object_ref.remote(task_id),
                            f"get_object_ref({task_id}) for cancellation recovery",
                        )
                        normalized_ref = _normalize_object_ref(object_ref)
                        if normalized_ref is not None:
                            tracker.track.remote(task_id, normalized_ref)
                        elif info.get("worker_submitted") is True:
                            tracker.recover_refless.remote(task_id, preserve_cancelled_submission=True)
                        elif not _has_finished_at(info.get("details")):
                            await self._record_finished_at(task_id)
                            await self._finish_cancellation(task_id)
                        continue
                    if _has_finished_at(info.get("details")):
                        continue
                    if state in _TERMINAL_STATES:
                        await self._record_finished_at(task_id)
                        continue
                    object_ref = await self._call_task_state(
                        lambda task_id=task_id: task_state_manager.get_object_ref.remote(task_id),
                        f"get_object_ref({task_id}) for completion recovery",
                    )
                    normalized_ref = _normalize_object_ref(object_ref)
                    if normalized_ref is not None:
                        tracker.track.remote(task_id, normalized_ref)
                    else:
                        tracker.recover_refless.remote(task_id)
            except Exception as exc:
                self._logger.warning("Failed to recover indexing completion tracking", error=str(exc))

    async def recover_refless(
        self,
        task_id: str,
        poll_interval: float = _REFLESS_RECOVERY_POLL_SECONDS,
        *,
        preserve_cancelled_submission: bool = False,
    ) -> None:
        """Watch a recovered active task whose ObjectRef has not been stored yet."""
        if task_id in self._tracked_task_ids:
            return
        self._tracked_task_ids.add(task_id)
        try:
            while True:
                task_state_manager = self._task_state_manager()
                details = await self._call_task_state(
                    lambda: task_state_manager.get_details.remote(task_id),
                    f"get_details({task_id}) for ref-less recovery",
                )
                if _has_finished_at(details):
                    return

                expire_refless = getattr(task_state_manager, "expire_refless_task_if_stale", None)
                expire_remote = getattr(expire_refless, "remote", None)
                if expire_remote is not None and await self._call_task_state(
                    lambda: expire_remote(task_id),
                    f"expire_refless_task_if_stale({task_id})",
                ):
                    await self._record_finished_at(task_id)
                    return

                state = await self._call_task_state(
                    lambda: task_state_manager.get_state.remote(task_id),
                    f"get_state({task_id}) for ref-less recovery",
                )
                object_ref = await self._call_task_state(
                    lambda: task_state_manager.get_object_ref.remote(task_id),
                    f"get_object_ref({task_id}) for ref-less recovery",
                )
                normalized_ref = _normalize_object_ref(object_ref)
                if normalized_ref is not None:
                    ref = normalized_ref["ref"]
                    await asyncio.gather(ref, return_exceptions=True)
                    await self._record_finished_at(task_id)
                    await self._finish_cancellation(task_id)
                    return

                if state == "CANCELLED" and preserve_cancelled_submission:
                    if await self._has_unsettled_cancelled_worker(task_state_manager, task_id):
                        await asyncio.sleep(poll_interval)
                        continue
                    await self._record_finished_at(task_id)
                    await self._finish_cancellation(task_id)
                    return

                if state in _TERMINAL_STATES:
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
        details = await self._call_task_state(
            lambda: task_state_manager.get_details.remote(task_id),
            f"get_details({task_id}) for completion timestamp",
        )
        if not isinstance(details, dict) or _has_finished_at(details):
            return

        metadata = details.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata[TASK_FINISHED_AT_METADATA_KEY] = _utc_now_iso()
        await self._call_task_state(
            lambda: task_state_manager.set_details.remote(
                task_id,
                file_id=details.get("file_id"),
                partition=details.get("partition"),
                metadata=metadata,
                user_id=details.get("user_id"),
            ),
            f"set_finished_at({task_id})",
        )

    async def _finish_cancellation(self, task_id: str) -> None:
        try:
            task_state_manager = self._task_state_manager()
            finish_cancellation = getattr(task_state_manager, "finish_cancellation", None)
            remote = getattr(finish_cancellation, "remote", None)
            if remote is not None:
                await self._call_task_state(lambda: remote(task_id), f"finish_cancellation({task_id})")
        except Exception as exc:
            self._logger.warning(
                "Failed to finalize indexing task cancellation",
                task_id=task_id,
                error=str(exc),
            )

    async def _has_unsettled_cancelled_worker(self, task_state_manager: Any, task_id: str) -> bool:
        method = getattr(task_state_manager, "has_unsettled_cancelled_worker", None)
        remote = getattr(method, "remote", None)
        if remote is None:
            # A mixed-version actor cannot prove settlement. Keep the claim
            # fenced until a compatible TaskStateManager is available.
            return True
        return bool(
            await self._call_task_state(
                lambda: remote(task_id),
                f"has_unsettled_cancelled_worker({task_id})",
            )
        )

    def _task_state_manager(self) -> Any:
        return ray.get_actor("TaskStateManager", namespace=self._namespace)

    async def _call_task_state(self, submit: Any, task_description: str) -> Any:
        return await call_ray_actor_method_with_timeout(
            submit,
            timeout=_TASK_STATE_CALL_TIMEOUT_SECONDS,
            task_description=task_description,
        )


TaskCompletionTrackerActor = ray.remote(max_restarts=-1, max_task_retries=-1, max_concurrency=1000)(
    TaskCompletionTracker
)


def _has_finished_at(details: Any) -> bool:
    if not isinstance(details, dict):
        return False
    metadata = details.get("metadata")
    return isinstance(metadata, dict) and TASK_FINISHED_AT_METADATA_KEY in metadata


def _normalize_object_ref(object_ref: Any) -> dict[str, Any] | None:
    ref = object_ref.get("ref") if isinstance(object_ref, dict) else object_ref
    return {"ref": ref} if ref is not None else None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["TaskCompletionTracker", "TaskCompletionTrackerActor"]
