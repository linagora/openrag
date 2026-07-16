from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import ray
from core.utils.logging import get_logger
from services.workers.ray_utils import call_ray_actor_with_timeout

logger = get_logger()

_REF_WAIT_INTERVAL = 0.05


async def cancel_active_indexing_tasks(
    task_state_manager: Any,
    *,
    partition: str,
    file_id: str | None = None,
    timeout: float = 60.0,
) -> int:
    """Cancel queued/running indexing tasks matching a partition or file."""
    try:
        remote = task_state_manager.get_matching_active_task_refs.remote
    except AttributeError:
        logger.warning(
            "TaskStateManager does not expose active-task lookup; delete will continue without task cancellation",
            partition=partition,
            file_id=file_id,
        )
        return 0

    deadline = monotonic() + timeout
    cancelled = 0
    while True:
        remaining = _remaining_timeout(deadline, partition=partition, file_id=file_id)
        matches = await call_ray_actor_with_timeout(
            future=remote(partition=partition, file_id=file_id),
            timeout=remaining,
            task_description=f"get_matching_active_task_refs({partition}, {file_id})",
        )
        pending_without_ref: list[str] = []
        for task_id, object_ref in matches.items():
            ref = _task_ref(object_ref)
            if ref is None:
                pending_without_ref.append(task_id)
                continue
            try:
                ray.cancel(ref, recursive=True)
            except Exception as exc:
                logger.warning(
                    "Failed to cancel active indexing task",
                    task_id=task_id,
                    partition=partition,
                    file_id=file_id,
                    error=str(exc),
                )
                raise RuntimeError(f"Failed to cancel active indexing task {task_id}") from exc
            remaining = _remaining_timeout(deadline, partition=partition, file_id=file_id)
            await call_ray_actor_with_timeout(
                future=task_state_manager.set_state.remote(task_id, "CANCELLED"),
                timeout=remaining,
                task_description=f"set_state({task_id}, CANCELLED)",
            )
            cancelled += 1
        if not pending_without_ref:
            return cancelled
        logger.info(
            "Waiting for active indexing tasks to expose object refs before delete cleanup",
            partition=partition,
            file_id=file_id,
            task_ids=pending_without_ref,
        )
        await asyncio.sleep(
            min(
                _REF_WAIT_INTERVAL,
                _remaining_timeout(deadline, partition=partition, file_id=file_id),
            )
        )


def _task_ref(object_ref: Any) -> Any | None:
    return object_ref.get("ref") if isinstance(object_ref, dict) else object_ref


def _remaining_timeout(deadline: float, *, partition: str, file_id: str | None) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError(
            "Timed out waiting for active indexing tasks to become cancellable "
            f"before deleting partition={partition!r}, file_id={file_id!r}"
        )
    return remaining
