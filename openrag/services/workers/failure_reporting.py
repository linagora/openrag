"""Rolling-compatible task failure reporting."""

from __future__ import annotations

from typing import Any

from core.utils.exceptions import NoIndexableContentError, ValidationError

_REASON_METHOD = "set_failed_with_reason_if_not_cancelled"


def submit_task_failure(
    task_state_manager: Any,
    task_id: str,
    traceback_text: str,
    error_reason: str,
) -> Any:
    """Submit the richest failure transition supported by the retained actor."""
    method_names = getattr(task_state_manager, "_ray_actor_method_names", None)
    if isinstance(method_names, (frozenset, list, set, tuple)) and _REASON_METHOD in method_names:
        method = getattr(task_state_manager, _REASON_METHOD, None)
        remote = getattr(method, "remote", None)
        if remote is not None:
            return remote(task_id, traceback_text, error_reason)
    return task_state_manager.set_failed_if_not_cancelled.remote(task_id, traceback_text)


def log_task_failure(log: Any, exc: BaseException, error_reason: str) -> None:
    """Log a failed indexing task through *log*, bound to its file, partition and task.

    No indexable content or invalid input is the file's outcome, not an
    operator alert: WARNING without traceback. Anything else is ERROR with the
    traceback. Not keyed on the 4xx status: Milvus and embedding errors are 422.
    """
    if isinstance(exc, (NoIndexableContentError, ValidationError)):
        log.warning("File indexing failed.", error_reason=error_reason)
    else:
        log.opt(exception=exc).error("File indexing failed.", error_reason=error_reason)
