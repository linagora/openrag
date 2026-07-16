from __future__ import annotations

from typing import Any

import ray
from core.utils.logging import get_logger
from services.workers.ray_utils import call_ray_actor_with_timeout

logger = get_logger()


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

    matches = await call_ray_actor_with_timeout(
        future=remote(partition=partition, file_id=file_id),
        timeout=timeout,
        task_description=f"get_matching_active_task_refs({partition}, {file_id})",
    )
    cancelled = 0
    for task_id, object_ref in matches.items():
        ref = object_ref.get("ref") if isinstance(object_ref, dict) else object_ref
        if ref is not None:
            try:
                ray.cancel(ref, recursive=True)
            except Exception:
                logger.warning(
                    "Failed to cancel active indexing task",
                    task_id=task_id,
                    partition=partition,
                    file_id=file_id,
                )
        await call_ray_actor_with_timeout(
            future=task_state_manager.set_state.remote(task_id, "CANCELLED"),
            timeout=timeout,
            task_description=f"set_state({task_id}, CANCELLED)",
        )
        cancelled += 1
    return cancelled
