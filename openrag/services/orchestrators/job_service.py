"""JobService — task-queue queries (Phase 8D.2).

Reads job state from the durable ``jobs`` table (``JobRepository``) and falls
back to the ``TaskStateManager`` Ray actor. Aggregation/filtering (the
active-status rollup, the per-status counts, the ``?task_status=`` filter) is
business logic and lives here; ``request.url_for`` link building stays in the
thin router (HTTP transport).

Issue #660 inverted the roles: Postgres is the source of truth and the actor is
a hot cache that evicts settled tasks and is wiped by a restart, so reading the
actor first would make completed work vanish from the queue views. The actor is
kept as the fallback for the two cases where the durable store cannot answer —
it is not wired (no job repository) or it is unreachable — because a degraded,
restart-local view of the queue beats a 500.

This is the one orchestrator that legitimately keeps Ray remote calls
during the shim — 8H verification explicitly excepts JobService
wrapping ``TaskStateManager``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from core.ports.job_repo import ACTIVE_JOB_STATES as _ACTIVE_STATES
from core.utils.logging import get_logger

logger = get_logger()

# Upper bound on a single ``list_tasks`` page against the durable store. The
# route is unpaginated (it used to read an in-memory dict), so without this a
# deployment with a full retention window would try to serialize every retained
# job into one response.
_LIST_LIMIT = 1000


class JobService:
    """Queue/worker introspection over the durable job store."""

    def __init__(self, task_state_manager: Any, timeout: float = 60.0, job_repo: Any = None) -> None:
        self._tsm = task_state_manager
        self._timeout = timeout
        self._job_repo = job_repo

    async def _call(self, future: Any, task_description: str) -> Any:
        """Route TaskStateManager calls through the centralized Ray helper.

        Direct ``.remote()`` awaits would bypass timeout/cancellation
        handling and can stall the queue APIs under Ray degradation. The
        canonical helper lives in ``services.workers.ray_utils``
        (``components.ray_utils`` is a backward-compat re-export).
        """
        from services.workers.ray_utils import call_ray_actor_with_timeout

        return await call_ray_actor_with_timeout(
            future=future,
            timeout=self._timeout,
            task_description=task_description,
        )

    async def _from_jobs(self, operation: str, call: Any) -> Any:
        """Run a durable read, or return ``None`` to signal "use the cache".

        Any repository failure is a fallback trigger, not a request failure: the
        actor still holds recent state, and a queue view is a diagnostic surface
        — the moment it 500s is exactly the moment it is being looked at.
        """
        if self._job_repo is None:
            return None
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 - fall back to the in-memory cache
            logger.warning(
                "Durable job read failed; falling back to the in-memory task cache",
                operation=operation,
                error=str(exc),
            )
            return None

    @staticmethod
    def _job_details(job: Any) -> dict:
        """Project a durable job onto the actor's ``details`` shape.

        The two read paths must be indistinguishable to callers — the API
        contract (and ``require_task_owner``) predates the durable store.
        """
        return {
            "file_id": job.file_id,
            "partition": job.partition,
            "metadata": job.job_metadata,
            "user_id": job.user_id,
        }

    @staticmethod
    def _format_pool_info(worker_info: dict[str, int]) -> dict[str, int]:
        """Condense ``SerializerQueue.pool_info()`` into the API shape."""
        return {
            "total_slots": worker_info["total_capacity"],
            "pool_size": worker_info["pool_size"],
            "max_per_actor": worker_info["max_tasks_per_worker"],
        }

    async def get_queue_info(self) -> dict:
        status_counts = await self._from_jobs("count_by_status", lambda: self._job_repo.count_by_status())
        if status_counts is None:
            all_states: dict = await self._call(self._tsm.get_all_states.remote(), "get_all_states")
            status_counts = Counter(all_states.values())

        active = {s: status_counts.get(s, 0) for s in _ACTIVE_STATES}
        task_summary = {
            "active": sum(active.values()),
            "active_statuses": active,
            "total_cancelled": status_counts.get("CANCELLED", 0),
            "total_completed": status_counts.get("COMPLETED", 0),
            "total_failed": status_counts.get("FAILED", 0),
        }

        worker_info = await self._call(self._tsm.get_pool_info.remote(), "get_pool_info")
        return {"workers": self._format_pool_info(worker_info), "tasks": task_summary}

    async def list_tasks(
        self,
        *,
        is_admin: bool,
        user_id: int | None,
        task_status: str | None = None,
    ) -> list[dict]:
        """Return ``{task_id, state, details}`` rows, filtered.

        - admins see every task; regular users only their own
        - ``task_status='active'`` → QUEUED|SERIALIZING|CHUNKING|INSERTING
        - any other value → exact match (case-insensitive)
        - ``None`` → all tasks

        The router decorates each row with the status / error URLs.
        """
        if not is_admin and user_id is None:
            # Fail closed. list_jobs(user_id=None) means "every job", so an
            # anonymous non-admin would otherwise be handed the whole table.
            # Unreachable today (the HTTP path always resolves an id), but the
            # unset default of the MCP _USER_ID ContextVar is exactly None,
            # so the escalating value is one wiring change away.
            return []
        jobs = await self._from_jobs(
            "list_jobs",
            lambda: self._job_repo.list_jobs(
                status=task_status,
                limit=_LIST_LIMIT,
                user_id=None if is_admin else user_id,
            ),
        )
        if jobs is not None:
            # Filtering happened in SQL; the fallback below has to do it itself.
            return [{"task_id": j.id, "state": j.status.value, "details": self._job_details(j)} for j in jobs]

        if is_admin:
            all_info: dict[str, dict] = await self._call(self._tsm.get_all_info.remote(), "get_all_info")
        else:
            all_info = await self._call(self._tsm.get_all_user_info.remote(user_id), f"get_all_user_info({user_id})")

        if task_status is None:
            filtered = list(all_info.items())
        elif task_status.lower() == "active":
            active_states = set(_ACTIVE_STATES)
            filtered = [(tid, i) for tid, i in all_info.items() if i["state"] in active_states]
        else:
            filtered = [(tid, i) for tid, i in all_info.items() if i["state"].lower() == task_status.lower()]

        return [{"task_id": tid, "state": i["state"], "details": i["details"]} for tid, i in filtered]

    async def get_user_pending_task_count(self, user_id: int | None) -> int:
        """Pending (not-yet-completed) indexing tasks for one user.

        Used by UserService for the quota-usage block of ``/users/info``
        (the legacy router called the actor directly from the handler).

        Deliberately *not* served from the durable store, unlike every other
        read here. This count gates uploads (``check_user_file_quota``), and a
        job row only leaves the active states when a worker writes a terminal
        transition — so a job orphaned by a crash mid-dispatch would occupy the
        user's quota permanently (retention only sweeps terminal rows). The
        in-memory count is wrong in the opposite, self-healing direction: a
        restart clears it. Moving this to Postgres is safe once orphaned
        in-flight jobs are reconciled at startup, which #664 tracks together
        with the rest of the quota TOCTOU fix.
        """
        return await self._call(
            self._tsm.get_user_pending_task_count.remote(user_id),
            f"get_user_pending_task_count({user_id})",
        )

    async def get_task_details(self, task_id: str) -> dict | None:
        """Return task details for ownership checks and status routes."""
        job = await self._from_jobs("get_job", lambda: self._job_repo.get_job(task_id))
        if job is not None:
            return self._job_details(job)
        return await self._call(
            self._tsm.get_details.remote(task_id),
            f"get_details({task_id})",
        )


__all__ = ["JobService"]
