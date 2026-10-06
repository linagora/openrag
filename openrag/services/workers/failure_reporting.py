"""Rolling-compatible task failure reporting."""

from __future__ import annotations

from typing import Any

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
    method_names = getattr(task_state_manager, "_ray_actor_method_names", None)
    if isinstance(method_names, (frozenset, list, set, tuple)) and stage_timings is not None:
        if _TIMED_REASON_METHOD in method_names:
            method = getattr(task_state_manager, _TIMED_REASON_METHOD, None)
            remote = getattr(method, "remote", None)
            if remote is not None:
                return remote(task_id, traceback_text, error_reason, stage_timings)
    if isinstance(method_names, (frozenset, list, set, tuple)) and _REASON_METHOD in method_names:
        method = getattr(task_state_manager, _REASON_METHOD, None)
        remote = getattr(method, "remote", None)
        if remote is not None:
            return remote(task_id, traceback_text, error_reason)
    if isinstance(method_names, (frozenset, list, set, tuple)) and stage_timings is not None:
        if _TIMED_METHOD in method_names:
            method = getattr(task_state_manager, _TIMED_METHOD, None)
            remote = getattr(method, "remote", None)
            if remote is not None:
                return remote(task_id, traceback_text, stage_timings)
    return task_state_manager.set_failed_if_not_cancelled.remote(task_id, traceback_text)
