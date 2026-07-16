from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import ray
from core.utils.logging import get_logger
from ray.exceptions import TaskCancelledError
from services.workers.ray_utils import call_ray_actor_with_timeout

logger = get_logger()

_ACTIVE_INDEXING_STATES = frozenset({"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"})
_REF_WAIT_INTERVAL = 0.05
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
    deadline = monotonic() + timeout
    cancelled = 0
    while True:
        matches = await _get_matching_active_task_refs(
            task_state_manager,
            deadline=deadline,
            partition=partition,
            file_id=file_id,
        )
        cancelled_now, pending_without_ref = await _cancel_refs(
            task_state_manager,
            matches,
            deadline=deadline,
            partition=partition,
            file_id=file_id,
        )
        cancelled += cancelled_now
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
            final_matches = await _get_matching_active_task_refs(
                task_state_manager,
                deadline=deadline,
                partition=partition,
                file_id=file_id,
                final=True,
            )
            cancelled_now, pending_without_ref = await _cancel_refs(
                task_state_manager,
                final_matches,
                deadline=deadline,
                partition=partition,
                file_id=file_id,
            )
            cancelled += cancelled_now
            if not pending_without_ref:
                return cancelled
            await _mark_ref_less_tasks_failed(
                task_state_manager,
                pending_without_ref,
                deadline=deadline,
                partition=partition,
                file_id=file_id,
            )
            return cancelled
        await asyncio.sleep(_REF_WAIT_INTERVAL)


async def _get_matching_active_task_refs(
    task_state_manager: Any,
    *,
    deadline: float,
    partition: str,
    file_id: str | None,
    final: bool = False,
) -> dict[str, Any]:
    get_matching = getattr(task_state_manager, "get_matching_active_task_refs", None)
    remote = getattr(get_matching, "remote", None)
    suffix = " final" if final else ""
    if remote is not None:
        return await call_ray_actor_with_timeout(
            future=remote(partition=partition, file_id=file_id),
            timeout=_remaining_timeout(deadline, partition=partition, file_id=file_id),
            task_description=f"get_matching_active_task_refs({partition}, {file_id}){suffix}",
        )
    return await _get_matching_active_task_refs_legacy(
        task_state_manager,
        deadline=deadline,
        partition=partition,
        file_id=file_id,
        final=final,
    )


async def _get_matching_active_task_refs_legacy(
    task_state_manager: Any,
    *,
    deadline: float,
    partition: str,
    file_id: str | None,
    final: bool,
) -> dict[str, Any]:
    get_all_info = getattr(task_state_manager, "get_all_info", None)
    get_all_info_remote = getattr(get_all_info, "remote", None)
    if get_all_info_remote is None:
        logger.warning(
            "TaskStateManager does not expose active-task lookup; refusing delete cleanup",
            partition=partition,
            file_id=file_id,
        )
        raise RuntimeError("TaskStateManager does not expose active-task lookup for delete cleanup")

    suffix = " final" if final else ""
    all_info = await call_ray_actor_with_timeout(
        future=get_all_info_remote(),
        timeout=_remaining_timeout(deadline, partition=partition, file_id=file_id),
        task_description=f"get_all_info_for_active_task_refs({partition}, {file_id}){suffix}",
    )
    if not isinstance(all_info, dict):
        raise RuntimeError("TaskStateManager returned invalid task info for delete cleanup")

    get_object_ref = getattr(task_state_manager, "get_object_ref", None)
    get_object_ref_remote = getattr(get_object_ref, "remote", None)
    matches: dict[str, Any] = {}
    for task_id, info in all_info.items():
        if not isinstance(info, dict):
            continue
        if info.get("state") not in _ACTIVE_INDEXING_STATES:
            continue
        details = info.get("details") or {}
        if not isinstance(details, dict):
            continue
        if not details:
            object_ref = None
            if get_object_ref_remote is not None:
                object_ref = await call_ray_actor_with_timeout(
                    future=get_object_ref_remote(task_id),
                    timeout=_remaining_timeout(deadline, partition=partition, file_id=file_id),
                    task_description=f"get_object_ref({task_id}) for delete cleanup",
                )
            matches[task_id] = object_ref
            continue
        if details.get("partition") != partition:
            continue
        if file_id is not None and details.get("file_id") != file_id:
            continue
        object_ref = None
        if get_object_ref_remote is not None:
            object_ref = await call_ray_actor_with_timeout(
                future=get_object_ref_remote(task_id),
                timeout=_remaining_timeout(deadline, partition=partition, file_id=file_id),
                task_description=f"get_object_ref({task_id}) for delete cleanup",
            )
        matches[task_id] = object_ref
    return matches


async def _cancel_refs(
    task_state_manager: Any,
    matches: dict[str, Any],
    *,
    deadline: float,
    partition: str,
    file_id: str | None,
) -> tuple[int, list[str]]:
    cancelled = 0
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
    return cancelled, pending_without_ref


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
    deadline: float,
    partition: str,
    file_id: str | None,
) -> None:
    set_failed = getattr(task_state_manager, "set_failed_if_not_cancelled", None)
    if set_failed is not None:
        for task_id in task_ids:
            await call_ray_actor_with_timeout(
                future=set_failed.remote(task_id, _STALE_REFLESS_TASK_ERROR),
                timeout=_remaining_timeout(deadline, partition=partition, file_id=file_id),
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
            timeout=_remaining_timeout(deadline, partition=partition, file_id=file_id),
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
