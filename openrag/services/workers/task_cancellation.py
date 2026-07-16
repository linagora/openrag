from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import ray
from core.utils.logging import get_logger
from ray.exceptions import TaskCancelledError
from services.workers.ray_utils import call_ray_actor_with_timeout

logger = get_logger()

_REF_WAIT_INTERVAL = 0.05
_STATE_UPDATE_TIMEOUT = 5.0
_STALE_REFLESS_TASK_ERROR = (
    "Indexing task never exposed a cancellable worker ref before delete cleanup; marking it failed as stale."
)


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
            await _wait_for_task_to_settle(
                ref,
                task_id=task_id,
                deadline=deadline,
                partition=partition,
                file_id=file_id,
            )
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
        remaining = deadline - monotonic()
        if remaining <= _REF_WAIT_INTERVAL:
            if remaining > 0:
                await asyncio.sleep(remaining)
            await _mark_ref_less_tasks_failed(
                task_state_manager,
                pending_without_ref,
                partition=partition,
                file_id=file_id,
            )
            return cancelled
        await asyncio.sleep(_REF_WAIT_INTERVAL)


def _task_ref(object_ref: Any) -> Any | None:
    return object_ref.get("ref") if isinstance(object_ref, dict) else object_ref


async def _wait_for_task_to_settle(
    ref: Any,
    *,
    task_id: str,
    deadline: float,
    partition: str,
    file_id: str | None,
) -> None:
    try:
        await call_ray_actor_with_timeout(
            future=ref,
            timeout=_remaining_timeout(deadline, partition=partition, file_id=file_id),
            task_description=f"wait_for_cancelled_indexing_task({task_id})",
        )
    except TaskCancelledError:
        return
    except TimeoutError as exc:
        raise TimeoutError(
            "Timed out waiting for active indexing task to settle after cancellation request "
            f"before deleting partition={partition!r}, file_id={file_id!r}, task_id={task_id!r}"
        ) from exc
    except Exception as exc:
        logger.info(
            "Active indexing task settled after cancellation request",
            task_id=task_id,
            partition=partition,
            file_id=file_id,
            result="failed",
            error=str(exc),
        )


async def _mark_ref_less_tasks_failed(
    task_state_manager: Any,
    task_ids: list[str],
    *,
    partition: str,
    file_id: str | None,
) -> None:
    set_failed = getattr(task_state_manager, "set_failed_if_not_cancelled", None)
    if set_failed is not None:
        for task_id in task_ids:
            await call_ray_actor_with_timeout(
                future=set_failed.remote(task_id, _STALE_REFLESS_TASK_ERROR),
                timeout=_STATE_UPDATE_TIMEOUT,
                task_description=f"set_failed_if_not_cancelled({task_id})",
            )
        logger.warning(
            "Marked stale ref-less indexing tasks as failed before delete cleanup",
            partition=partition,
            file_id=file_id,
            task_ids=task_ids,
        )
        return
    for task_id in task_ids:
        await call_ray_actor_with_timeout(
            future=task_state_manager.set_state.remote(task_id, "FAILED"),
            timeout=_STATE_UPDATE_TIMEOUT,
            task_description=f"set_state({task_id}, FAILED)",
        )
    logger.warning(
        "Marked stale ref-less indexing tasks as failed before delete cleanup",
        partition=partition,
        file_id=file_id,
        task_ids=task_ids,
    )


def _remaining_timeout(deadline: float, *, partition: str, file_id: str | None) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError(
            "Timed out waiting for active indexing tasks to become cancellable "
            f"before deleting partition={partition!r}, file_id={file_id!r}"
        )
    return remaining
