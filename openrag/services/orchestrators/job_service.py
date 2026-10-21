"""JobService — task-queue queries (Phase 8D.2).

Thin wrapper around the ``TaskStateManager`` Ray actor, extracted from
``routers/queue.py``. Aggregation/filtering (the active-status rollup,
the per-status counts, the ``?task_status=`` filter) is business logic
and lives here; ``request.url_for`` link building stays in the thin
router (HTTP transport).

The PostgreSQL job repository supplies history after the actor evicts a task;
while both sources still have it, state reconciliation prevents a stale active
row from hiding a terminal result in the live actor.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from core.models.catalog import (
    LEGACY_ACTIVE_INDEXING_STATES,
    TASK_CREATED_AT_METADATA_KEY,
    TASK_FINISHED_AT_METADATA_KEY,
    TERMINAL_TASK_STATES,
    normalize_degraded_stages,
    reconcile_task_state,
)
from core.utils.error_summary import summarize_task_error
from core.utils.logging import get_logger

logger = get_logger()

_ACTIVE_STATES = ("QUEUED", "SERIALIZING")
_DURABLE_TASK_LIMIT = 500
_TERMINAL_STATES = frozenset(state.value for state in TERMINAL_TASK_STATES)


class JobService:
    """Queue/worker introspection over durable history and live actor state."""

    def __init__(self, task_state_manager: Any, timeout: float = 60.0, *, job_repo: Any = None) -> None:
        self._tsm = task_state_manager
        self._timeout = timeout
        self._job_repo = job_repo

    async def _call(self, submit: Any, task_description: str) -> Any:
        """Route TaskStateManager calls through the centralized Ray helper.

        Direct ``.remote()`` awaits would bypass timeout/cancellation
        handling and can stall the queue APIs under Ray degradation. The
        canonical helper lives in ``services.workers.ray_utils``
        (``components.ray_utils`` is a backward-compat re-export).
        """
        from services.workers.ray_utils import retry_idempotent_ray_actor_method

        return await retry_idempotent_ray_actor_method(
            submit=submit,
            recovery_timeout=self._timeout,
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

    async def _actor_states(self) -> dict[str, str | None]:
        return await self._call(lambda: self._tsm.get_all_states.remote(), "get_all_states")

    async def _task_status_counts(self, all_states: dict[str, str | None]) -> Counter[str]:
        """Per-state task totals: durable counts first, reconciled with the live actor.

        Falls back to the actor alone only when the durable counts are
        unavailable. The in-flight states are counted by ``_active_task_counts``
        instead: adjusting a count taken at one moment with rows read at another
        can subtract a row the count never included.
        """
        status_counts = await self._durable_status_counts()
        if not status_counts:
            return Counter(all_states.values())

        durable_actor_states = await self._durable_task_states_for_ids(all_states)
        if durable_actor_states is None:
            return Counter(all_states.values())
        for task_id, actor_state in all_states.items():
            durable_state = durable_actor_states.get(task_id)
            if durable_state is None:
                status_counts[actor_state] += 1
                continue
            effective_state = reconcile_task_state(actor_state, durable_state)
            if effective_state != durable_state:
                status_counts[durable_state] -= 1
                status_counts[effective_state] += 1
        return status_counts

    async def _active_task_counts(self, all_states: dict[str, str | None]) -> dict[str, int]:
        """In-flight tasks by state, reconciled one task at a time.

        Durable rows are authoritative, so tasks a restarted actor has forgotten
        still count; a live terminal state still beats a stale active row. Each
        task is resolved to one state before anything is counted, so a row
        written between the reads can be counted late but never subtracted, and
        the result cannot go negative. Only in-flight rows are read — the
        metrics scrape calls this on every interval. Falls back to the actor
        alone when the durable rows cannot be read.
        """
        actor_active = {tid: state for tid, state in all_states.items() if state in _ACTIVE_STATES}
        if self._job_repo is None:
            return _count_active(actor_active.values())
        try:
            durable_active = await self._job_repo.get_job_states(statuses=list(_ACTIVE_STATES))
            # Live tasks the first read missed: written after it, or already settled.
            unseen = [tid for tid in actor_active if tid not in durable_active]
            durable_unseen = await self._job_repo.get_job_states(job_ids=unseen) if unseen else {}
        except Exception as exc:
            logger.warning("Failed to read durable in-flight jobs", error=str(exc))
            return _count_active(actor_active.values())

        effective = [reconcile_task_state(all_states.get(tid), state) for tid, state in durable_active.items()]
        effective += [reconcile_task_state(actor_active[tid], durable_unseen.get(tid)) for tid in unseen]
        return _count_active(effective)

    async def get_queue_info(self) -> dict:
        all_states = await self._actor_states()
        status_counts = await self._task_status_counts(all_states)
        active = await self._active_task_counts(all_states)
        task_summary = {
            "active": sum(active.values()),
            "active_statuses": active,
            "total_cancelled": status_counts.get("CANCELLED", 0),
            "total_completed": status_counts.get("COMPLETED", 0),
            "total_failed": status_counts.get("FAILED", 0),
        }

        worker_info = await self._call(lambda: self._tsm.get_pool_info.remote(), "get_pool_info")
        return {"workers": self._format_pool_info(worker_info), "tasks": task_summary}

    async def get_active_task_counts(self) -> dict[str, int]:
        """The ``active_statuses`` counts alone, without the worker-pool round trip.

        ``get_queue_info`` always fetches pool info too, which the metrics scrape
        never uses; that second Ray call would just eat into its timeout budget.
        The counts themselves come from the same durable-first path.
        """
        return await self._active_task_counts(await self._actor_states())

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

        # The actor only remembers live and recent tasks. Durable rows fill in
        # history it has evicted, and everything dispatched before a restart.
        # A second ID-scoped read is required for authority: a status-filtered
        # durable query can omit a row whose actor state is stale but whose
        # durable status is different.
        durable_info = await self._durable_task_info(
            is_admin=is_admin,
            user_id=user_id,
            task_status=task_status,
        )
        durable_actor_info = await self._durable_task_info_for_ids(all_info)
        all_info = {**durable_info, **all_info}
        if durable_actor_info is not None:
            for task_id, durable_info_for_actor in durable_actor_info.items():
                all_info[task_id] = _merge_durable_task_info(all_info[task_id], durable_info_for_actor)
        all_info = {task_id: {**info, "state": _public_task_state(info["state"])} for task_id, info in all_info.items()}

        if task_status is None:
            filtered = list(all_info.items())
        elif task_status.lower() == "active":
            active_states = set(_ACTIVE_STATES)
            filtered = [(tid, i) for tid, i in all_info.items() if i["state"] in active_states]
        else:
            filtered = [(tid, i) for tid, i in all_info.items() if i["state"].lower() == task_status.lower()]

        now = self._now()
        return [self._task_row(task_id, info, now=now, include_error_summary=is_admin) for task_id, info in filtered]

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _task_row(
        task_id: str,
        info: dict[str, Any],
        *,
        now: datetime,
        include_error_summary: bool,
    ) -> dict[str, Any]:
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

        row = {
            "task_id": task_id,
            "state": info["state"],
            "outcome": _task_outcome(info["state"], details),
            "details": details,
            "created_at": created_at,
            "duration_ms": duration_ms,
        }
        if include_error_summary and info["state"] == "FAILED":
            summary = summarize_task_error(info.get("error"), reason=info.get("error_reason"))
            if summary:
                row["error_summary"] = summary
        return row

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
        job = await self._durable_job(task_id)
        if job is not None:
            durable_info = _job_to_info(job)
            try:
                actor_state = await self._call(
                    lambda: self._tsm.get_state.remote(task_id),
                    f"get_state({task_id})",
                )
            except Exception as exc:
                logger.warning("Failed to read live task state", task_id=task_id, error=str(exc))
                actor_state = None
            try:
                actor_details = await self._call(
                    lambda: self._tsm.get_details.remote(task_id),
                    f"get_details({task_id})",
                )
            except Exception as exc:
                logger.warning("Failed to read live task details", task_id=task_id, error=str(exc))
                actor_details = None
            details = _merge_durable_task_info(
                {"state": actor_state, "details": actor_details or {}},
                durable_info,
            )["details"]
        else:
            details = await self._call(
                lambda: self._tsm.get_details.remote(task_id),
                f"get_details({task_id})",
            )
        if details is None:
            return None
        public_details, _, _ = _task_details(details)
        return public_details

    async def _durable_job(self, task_id: str) -> Any:
        if self._job_repo is None:
            return None
        try:
            return await self._job_repo.get_job(task_id)
        except Exception as exc:
            logger.warning("Failed to read durable job", task_id=task_id, error=str(exc))
            return None

    async def _durable_task_info(
        self,
        *,
        is_admin: bool,
        user_id: int | None,
        task_status: str | None,
    ) -> dict[str, dict]:
        if self._job_repo is None:
            return {}
        try:
            jobs = await self._job_repo.list_jobs(
                statuses=_durable_statuses(task_status),
                user_id=None if is_admin else user_id,
                limit=_DURABLE_TASK_LIMIT,
            )
        except Exception as exc:
            logger.warning("Failed to list durable jobs", error=str(exc))
            return {}
        return {job.id: _job_to_info(job) for job in jobs}

    async def _durable_task_info_for_ids(self, actor_info: dict[str, dict]) -> dict[str, dict] | None:
        if self._job_repo is None or not actor_info:
            return {}
        try:
            jobs = await self._job_repo.get_jobs(list(actor_info))
        except Exception as exc:
            logger.warning("Failed to read durable jobs for live task IDs", error=str(exc))
            return None
        return {job.id: _job_to_info(job) for job in jobs}

    async def _durable_task_states_for_ids(self, actor_states: dict[str, str | None]) -> dict[str, str] | None:
        if self._job_repo is None or not actor_states:
            return {}
        try:
            jobs = await self._job_repo.get_jobs(list(actor_states))
        except Exception as exc:
            logger.warning("Failed to read durable job states for live task IDs", error=str(exc))
            return None
        return {job.id: job.status.value for job in jobs}

    async def _durable_status_counts(self) -> Counter[str] | None:
        if self._job_repo is None:
            return None
        try:
            counts = await self._job_repo.count_jobs()
        except Exception as exc:
            logger.warning("Failed to count durable jobs", error=str(exc))
            return None
        return Counter(counts)


def _count_active(states: Any) -> dict[str, int]:
    counts = Counter(states)
    return {s: counts.get(s, 0) for s in _ACTIVE_STATES}


def _durable_statuses(task_status: str | None) -> list[str] | None:
    """The statuses the durable query should return, or ``None`` for all.

    The row limit applies to what the query returns, so an unfiltered read
    would hide older matches behind newer rows of every other status.
    """
    if task_status is None:
        return None
    if task_status.lower() == "active":
        return list(_ACTIVE_STATES)
    return [task_status.upper()]


def _job_to_info(job: Any) -> dict[str, Any]:
    """Render a durable row in the same shape the actor returns."""
    created_at = job.created_at.isoformat() if job.created_at else None
    completed_at = job.completed_at.isoformat() if job.completed_at else None
    state = job.status.value
    return {
        "state": state,
        "error": job.error,
        "error_reason": job.error_reason,
        "details": {
            "file_id": job.file_id,
            "partition": job.partition,
            "metadata": {"filename": job.filename} if job.filename else {},
            "user_id": job.user_id,
            "degraded_stages": job.degraded_stages,
        },
        "created_at": created_at,
        "duration_ms": _duration_ms(created_at, completed_at, state=state, now=datetime.now(UTC)),
    }


def _merge_durable_task_info(actor_info: dict[str, Any], durable_info: dict[str, Any]) -> dict[str, Any]:
    """Merge durable history without hiding a newer terminal actor state."""
    merged = {**actor_info, **durable_info}
    effective_state = reconcile_task_state(actor_info.get("state"), durable_info.get("state"))
    if effective_state is not None:
        merged["state"] = effective_state
    actor_terminal_wins = (
        actor_info.get("state") in _TERMINAL_STATES and durable_info.get("state") not in _TERMINAL_STATES
    )
    if actor_terminal_wins:
        merged["error"] = actor_info.get("error")
        merged["error_reason"] = actor_info.get("error_reason")
        merged.pop("duration_ms", None)
        if actor_info.get("duration_ms") is not None:
            merged["duration_ms"] = actor_info["duration_ms"]
    actor_details = actor_info.get("details")
    durable_details = durable_info.get("details")
    if not isinstance(actor_details, dict) or not isinstance(durable_details, dict):
        return merged

    details = {**actor_details, **durable_details}
    if actor_terminal_wins and "degraded_stages" in actor_details:
        details["degraded_stages"] = actor_details["degraded_stages"]
    actor_metadata = actor_details.get("metadata")
    durable_metadata = durable_details.get("metadata")
    if isinstance(actor_metadata, dict) or isinstance(durable_metadata, dict):
        details["metadata"] = {
            **(actor_metadata if isinstance(actor_metadata, dict) else {}),
            **(durable_metadata if isinstance(durable_metadata, dict) else {}),
        }
    merged["details"] = details
    return merged


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
    if "degraded_stages" in public_details:
        public_details["degraded_stages"] = normalize_degraded_stages(public_details["degraded_stages"])
    raw_metadata = public_details.get("metadata")
    if not isinstance(raw_metadata, dict):
        return public_details, None, None

    metadata = dict(raw_metadata)
    created_at = metadata.pop(TASK_CREATED_AT_METADATA_KEY, None)
    finished_at = metadata.pop(TASK_FINISHED_AT_METADATA_KEY, None)
    public_details["metadata"] = metadata
    return public_details, created_at, finished_at


def _task_outcome(state: str, details: dict[str, Any]) -> str:
    if state in _ACTIVE_STATES:
        return "active"
    if state == "COMPLETED" and details.get("degraded_stages"):
        return "completed_degraded"
    return state.lower()


def _public_task_state(state: str) -> str:
    # Detached pre-#721 actors may still emit these internal states during a
    # rolling deployment. Keep them out of the public contract while retaining
    # active filtering and cancellation behavior.
    return "SERIALIZING" if state in LEGACY_ACTIVE_INDEXING_STATES else state


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
