"""JobService — task-queue queries (Phase 8D.2).

Thin wrapper around the ``TaskStateManager`` Ray actor, extracted from
``routers/queue.py``. Aggregation/filtering (the active-status rollup,
the per-status counts, the ``?task_status=`` filter) is business logic
and lives here; ``request.url_for`` link building stays in the thin
router (HTTP transport).

This is the one orchestrator that legitimately keeps Ray remote calls
during the shim — 8H verification explicitly excepts JobService
wrapping ``TaskStateManager``. Phase 9 swaps the actor for a DB-backed
job repository (this service is the hook point for that P0 feature).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from core.models.catalog import (
    TASK_CREATED_AT_METADATA_KEY,
    TASK_FINISHED_AT_METADATA_KEY,
    TERMINAL_TASK_STATES,
)

_ACTIVE_STATES = ("QUEUED", "SERIALIZING")
_TERMINAL_STATES = frozenset(state.value for state in TERMINAL_TASK_STATES)


class JobService:
    """Queue/worker introspection over the TaskStateManager actor."""

    def __init__(self, task_state_manager: Any, timeout: float = 60.0) -> None:
        self._tsm = task_state_manager
        self._timeout = timeout

    async def _call(self, submit: Any, task_description: str) -> Any:
        """Route TaskStateManager calls through the centralized Ray helper.

        Direct ``.remote()`` awaits would bypass timeout/cancellation
        handling and can stall the queue APIs under Ray degradation. The
        canonical helper lives in ``services.workers.ray_utils``
        (``components.ray_utils`` is a backward-compat re-export).
        """
        from services.workers.ray_utils import call_ray_actor_method_with_timeout

        return await call_ray_actor_method_with_timeout(
            submit=submit,
            timeout=self._timeout,
            task_description=task_description,
        )

    @staticmethod
    def _format_pool_info(worker_info: dict[str, int]) -> dict[str, int]:
        """Condense ``SerializerQueue.pool_info()`` into the API shape."""
        return {
            "total_slots": worker_info["total_capacity"],
            "pool_size": worker_info["pool_size"],
            "max_per_actor": worker_info["max_tasks_per_worker"],
        }

    async def get_queue_info(self) -> dict:
        all_states: dict = await self._call(lambda: self._tsm.get_all_states.remote(), "get_all_states")
        status_counts = Counter(all_states.values())

        active = {s: status_counts.get(s, 0) for s in _ACTIVE_STATES}
        task_summary = {
            "active": sum(active.values()),
            "active_statuses": active,
            "total_cancelled": status_counts.get("CANCELLED", 0),
            "total_completed": status_counts.get("COMPLETED", 0),
            "total_failed": status_counts.get("FAILED", 0),
        }

        worker_info = await self._call(lambda: self._tsm.get_pool_info.remote(), "get_pool_info")
        return {"workers": self._format_pool_info(worker_info), "tasks": task_summary}

    async def list_tasks(
        self,
        *,
        is_admin: bool,
        user_id: int | None,
        task_status: str | None = None,
    ) -> list[dict]:
        """Return task rows with details and lifecycle timing, filtered.

        - admins see every task; regular users only their own
        - ``task_status='active'`` → QUEUED|SERIALIZING
        - any other value → exact match (case-insensitive)
        - ``None`` → all tasks

        The router decorates each row with the status / error URLs.
        """
        if is_admin:
            all_info: dict[str, dict] = await self._call(lambda: self._tsm.get_all_info.remote(), "get_all_info")
        else:
            all_info = await self._call(
                lambda: self._tsm.get_all_user_info.remote(user_id),
                f"get_all_user_info({user_id})",
            )

        if task_status is None:
            filtered = list(all_info.items())
        elif task_status.lower() == "active":
            active_states = set(_ACTIVE_STATES)
            filtered = [(tid, i) for tid, i in all_info.items() if i["state"] in active_states]
        else:
            filtered = [(tid, i) for tid, i in all_info.items() if i["state"].lower() == task_status.lower()]

        now = self._now()
        return [self._task_row(task_id, info, now=now) for task_id, info in filtered]

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _task_row(task_id: str, info: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        details, fallback_created_at, fallback_finished_at = _task_details(info.get("details"))

        created_at = info.get("created_at") or fallback_created_at
        duration_ms = info.get("duration_ms")
        if duration_ms is None:
            duration_ms = _duration_ms(
                created_at,
                fallback_finished_at,
                state=info.get("state"),
                now=now,
            )

        return {
            "task_id": task_id,
            "state": info["state"],
            "details": details,
            "created_at": created_at,
            "duration_ms": duration_ms,
        }

    async def get_user_pending_task_count(self, user_id: int | None) -> int:
        """Pending (not-yet-completed) indexing tasks for one user.

        Used by UserService for the quota-usage block of ``/users/info``
        (the legacy router called the actor directly from the handler).
        """
        return await self._call(
            lambda: self._tsm.get_user_pending_task_count.remote(user_id),
            f"get_user_pending_task_count({user_id})",
        )

    async def get_task_details(self, task_id: str) -> dict | None:
        """Return task details for ownership checks and status routes."""
        details = await self._call(
            lambda: self._tsm.get_details.remote(task_id),
            f"get_details({task_id})",
        )
        if details is None:
            return None
        public_details, _, _ = _task_details(details)
        return public_details


def _duration_ms(
    created_at: Any,
    finished_at: Any,
    *,
    state: Any,
    now: datetime,
) -> int | None:
    created = _parse_timestamp(created_at)
    if created is None:
        return None
    finished = _parse_timestamp(finished_at)
    if finished is None:
        if state in _TERMINAL_STATES:
            return None
        finished = now
    return max(0, int((finished - created).total_seconds() * 1000))


def _task_details(details: Any) -> tuple[dict[str, Any], Any, Any]:
    public_details = dict(details) if isinstance(details, dict) else {}
    raw_metadata = public_details.get("metadata")
    if not isinstance(raw_metadata, dict):
        return public_details, None, None

    metadata = dict(raw_metadata)
    created_at = metadata.pop(TASK_CREATED_AT_METADATA_KEY, None)
    finished_at = metadata.pop(TASK_FINISHED_AT_METADATA_KEY, None)
    public_details["metadata"] = metadata
    return public_details, created_at, finished_at


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["JobService"]
