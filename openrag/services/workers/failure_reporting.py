"""Rolling-compatible task failure reporting."""

from __future__ import annotations

from typing import Any

from services.workers.ray_utils import get_ray_actor_method

_REASON_METHOD = "set_failed_with_reason_if_not_cancelled"
_TIMED_REASON_METHOD = "set_failed_with_reason_and_stage_timings_if_not_cancelled"
_TIMED_METHOD = "set_failed_with_stage_timings_if_not_cancelled"


def submit_task_failure(
    task_state_manager: Any,
    task_id: str,
    traceback_text: str,
    error_reason: str,
    stage_timings: dict[str, float] | None = None,
) -> Any:
    """Submit the richest failure transition supported by the retained actor."""
    method = get_ray_actor_method(task_state_manager, _TIMED_REASON_METHOD) if stage_timings is not None else None
    if method is not None:
        return method.remote(task_id, traceback_text, error_reason, stage_timings)
    method = get_ray_actor_method(task_state_manager, _REASON_METHOD)
    if method is not None:
        return method.remote(task_id, traceback_text, error_reason)
    method = get_ray_actor_method(task_state_manager, _TIMED_METHOD) if stage_timings is not None else None
    if method is not None:
        return method.remote(task_id, traceback_text, stage_timings)
    return task_state_manager.set_failed_if_not_cancelled.remote(task_id, traceback_text)
