from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import ray
from core.models.catalog import TERMINAL_TASK_STATES, DocumentStatus
from core.utils.text import MAX_ERROR_TEXT_CHARS, truncate_error_text

ACTIVE_INDEXING_STATES = frozenset({"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"})
TERMINAL_INDEXING_STATES = frozenset({"COMPLETED", "FAILED"})
PENDING_TASK_DETAILS = "__openrag_pending_task_details__"

try:
    from core.config import load_config as _load_config

    _cfg = _load_config()
    _POOL_SIZE: int = _cfg.ray.indexer.pool_size
    _MAX_TASKS_PER_WORKER: int = _cfg.ray.indexer.max_tasks_per_worker
except (ImportError, AttributeError) as _cfg_err:
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "Could not load ray config for TaskStateManager pool info: %s — using defaults", _cfg_err
    )
    _POOL_SIZE = 1
    _MAX_TASKS_PER_WORKER = 1


# This actor is created with ``lifetime="detached"`` (see ``bootstrap.py``), so it
# outlives the API process and used to be insert-only: every file ever dispatched
# left a permanent ``TaskInfo`` (plus a full traceback on failure) until the actor
# OOM-ed (issue #660). Postgres now holds the durable record, which frees this
# actor to be what its callers actually need — a hot cache of recent tasks.
#
# Only *terminal* tasks are evictable: an in-flight task still owns the
# ``object_ref`` that ``cancel_task`` needs, and that ref is not serializable, so
# it cannot live anywhere but here. In-flight tasks are self-limiting (the pool
# has bounded capacity and every task eventually settles), terminal ones are not.
#
# Both bounds apply, and both are enforced lazily: the sweep runs only when a
# task settles, never on a timer (this is a Ray actor; a background loop would be
# another thing to own). The cap is therefore the real memory guarantee — it is
# checked exactly when growth happens. The TTL only retires stale entries once
# *some* task settles, so a fully idle deployment keeps its last few terminal
# tasks cached indefinitely. That is harmless: a terminal state is immutable, so
# a stale read is not a wrong read, and the cap still bounds the memory.
# Reads that miss fall back to Postgres (``WorkerDispatcher.get_task_state`` /
# ``JobService``), which is the durable record either way.
_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_MAX_TERMINAL_TASKS = 2000
_TERMINAL_TTL_SECONDS = 3600.0
_MAX_ERROR_CHARS = MAX_ERROR_TEXT_CHARS


@dataclass
class TaskInfo:
    state: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    object_ref: ray.ObjectRef | None = None
    # #664. ``quota_reserved`` records that admission charged one
    # ``users.file_count`` slot for this task; ``quota_release_claimed`` is the
    # one-shot token that decides *who* gives it back. Kept on the record rather
    # than in ``details`` because ``details`` is surfaced in API responses.
    quota_reserved: bool = False
    quota_release_claimed: bool = False


@ray.remote(concurrency_groups={"set": 1000, "get": 1000, "queue_info": 1000})
class TaskStateManager:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskInfo] = {}
        self.user_index: dict[int | None, set[str]] = {}
        self.file_delete_fences: dict[tuple[str, str], int] = {}
        # task_id -> monotonic timestamp of the terminal transition, in
        # insertion order so eviction is FIFO (oldest settled task first).
        self.terminal_at: OrderedDict[str, float] = OrderedDict()
        self.lock = asyncio.Lock()

    async def _ensure_task(self, task_id: str) -> TaskInfo:
        """Create the entry for a task we are hearing about for the first time.

        The call that legitimately means "new" is the dispatcher's opening
        ``QUEUED`` write, the first write for a task id (before
        ``set_details``/``set_object_ref``).

        ``set_state`` is *not* dispatcher-only, though: the worker also writes
        ``SERIALIZING`` and ``COMPLETED`` through it. That is safe today only
        because of ordering -- the worker's first write happens long before the
        task can be terminal, and eviction only ever removes *terminal* entries,
        so there is nothing evicted for it to resurrect. Anything that breaks
        that ordering (a ``set_state`` after a terminal transition) reopens the
        leak this guard exists to close: a resurrected entry with a
        *non-terminal* state never re-enters ``terminal_at`` and is never
        evictable again.

        Making creation opt-in (``create=True``, dispatcher only) is the durable
        fix, but it changes the signature of a **detached** actor method --
        ``get_or_create_actor(..., lifetime="detached")`` keeps the previous
        instance alive across an API deploy, so a new dispatcher would call an
        old actor and every dispatch would fail on the unexpected keyword. It
        therefore has to be sequenced with a deliberate actor restart rather
        than shipped as a plain code change (tracked in #676).

        Every *other* writer must go through :meth:`_live_task`. Creating an
        entry from a late write would resurrect an evicted task with
        ``state=None``, which never re-enters ``terminal_at`` and is therefore
        never evictable again — an unbounded leak on a detached actor, i.e. the
        exact failure #660 exists to fix.
        """
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskInfo()
        return self.tasks[task_id]

    def _record_details(
        self,
        task_id: str,
        info: TaskInfo,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
    ) -> None:
        info.details = {
            "file_id": file_id,
            "partition": partition,
            "metadata": metadata,
            "user_id": user_id,
        }
        self.user_index.setdefault(user_id, set()).add(task_id)

    def _file_delete_fenced(self, *, partition: str | None, file_id: str | None) -> bool:
        if partition is None or file_id is None:
            return False
        return self.file_delete_fences.get((partition, file_id), 0) > 0

    @ray.method(concurrency_group="set")
    async def begin_file_delete(self, *, partition: str, file_id: str) -> None:
        async with self.lock:
            key = (partition, file_id)
            self.file_delete_fences[key] = self.file_delete_fences.get(key, 0) + 1

    @ray.method(concurrency_group="set")
    async def end_file_delete(self, *, partition: str, file_id: str) -> None:
        async with self.lock:
            key = (partition, file_id)
            remaining = self.file_delete_fences.get(key, 0) - 1
            if remaining > 0:
                self.file_delete_fences[key] = remaining
            else:
                self.file_delete_fences.pop(key, None)

    def _live_task(self, task_id: str) -> TaskInfo | None:
        """The entry for ``task_id``, or ``None`` if it is unknown or evicted.

        A write for a task that is no longer cached is dropped: the durable
        ``jobs`` row is the record of what happened, and this actor is only a
        hot cache of recent tasks. See :meth:`_ensure_task` for why recreating
        it here would be a leak.
        """
        return self.tasks.get(task_id)

    def _mark_terminal(self, task_id: str, state: str | None) -> None:
        """Record (or clear) a task's terminal transition, then evict.

        Called with ``self.lock`` held, from every state write.
        """
        if state in _TERMINAL_STATES:
            self.terminal_at[task_id] = time.monotonic()
            self.terminal_at.move_to_end(task_id)
            self._evict_terminal()
        else:
            # A task that leaves a terminal state (a re-dispatch reusing the id)
            # must stop being a candidate for eviction.
            self.terminal_at.pop(task_id, None)

    def _evict_terminal(self) -> None:
        """Drop terminal tasks that are over the cap or past the TTL."""
        now = time.monotonic()
        while self.terminal_at:
            task_id, settled_at = next(iter(self.terminal_at.items()))
            over_cap = len(self.terminal_at) > _MAX_TERMINAL_TASKS
            expired = now - settled_at > _TERMINAL_TTL_SECONDS
            if not (over_cap or expired):
                # FIFO: the head is the oldest, so nothing behind it can qualify.
                break
            self.terminal_at.popitem(last=False)
            self._forget(task_id)

    def _forget(self, task_id: str) -> None:
        info = self.tasks.pop(task_id, None)
        if info is None:
            return
        user_id = info.details.get("user_id")
        task_ids = self.user_index.get(user_id)
        if task_ids is None:
            return
        task_ids.discard(task_id)
        if not task_ids:
            del self.user_index[user_id]

    @ray.method(concurrency_group="set")
    async def set_state(self, task_id: str, state: str) -> None:
        async with self.lock:
            info = await self._ensure_task(task_id)
            if info.state == DocumentStatus.CANCELLED and state != DocumentStatus.CANCELLED:
                return
            info.state = state
            self._mark_terminal(task_id, state)

    @ray.method(concurrency_group="set")
    async def set_error(self, task_id: str, tb_str: str) -> None:
        async with self.lock:
            info = self._live_task(task_id)
            if info is None:
                return
            info.error = truncate_error_text(tb_str, _MAX_ERROR_CHARS)

    @ray.method(concurrency_group="set")
    async def set_failed_if_not_cancelled(self, task_id: str, tb_str: str) -> bool:
        """Atomically set state to FAILED and record the traceback, unless already CANCELLED."""
        async with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state == "CANCELLED":
                return False
            info.state = "FAILED"
            info.error = truncate_error_text(tb_str, _MAX_ERROR_CHARS)
            self._mark_terminal(task_id, "FAILED")
            return True

    @ray.method(concurrency_group="set")
    async def set_cancelled_if_active(self, task_id: str) -> bool:
        async with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state in TERMINAL_TASK_STATES:
                return False
            info.state = "CANCELLED"
            # CANCELLED is terminal, so it must register like every other
            # terminal write does. Skipping this leaves the entry retained
            # forever: eviction is driven entirely off ``terminal_at``, and
            # nothing writes this task's state again -- ray.cancel raises
            # CancelledError, a BaseException that ``process_file``'s
            # ``except Exception`` never catches. That is precisely the
            # unbounded growth #660 exists to fix.
            self._mark_terminal(task_id, "CANCELLED")
            return True

    @ray.method(concurrency_group="set")
    async def set_details(
        self,
        task_id: str,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
        quota_reserved: bool = False,
    ) -> None:
        async with self.lock:
            info = self._live_task(task_id)
            if info is None:
                # Dropping the details also keeps ``user_index`` from growing an
                # entry that ``_forget`` will never be able to clean up.
                return
            self._record_details(
                task_id,
                info,
                file_id=file_id,
                partition=partition,
                metadata=metadata,
                user_id=user_id,
            )
            info.quota_reserved = quota_reserved

    @ray.method(concurrency_group="set")
    async def set_queued_details(
        self,
        task_id: str,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
        quota_reserved: bool = False,
    ) -> bool:
        async with self.lock:
            info = await self._ensure_task(task_id)
            if info.state == DocumentStatus.CANCELLED:
                return False
            self._record_details(
                task_id,
                info,
                file_id=file_id,
                partition=partition,
                metadata=metadata,
                user_id=user_id,
            )
            info.quota_reserved = quota_reserved
            if self._file_delete_fenced(partition=partition, file_id=file_id):
                info.state = "CANCELLED"
                return False
            info.state = "QUEUED"
            return True

    @ray.method(concurrency_group="set")
    async def claim_quota_release(self, task_id: str) -> bool:
        """Hand the task's reserved file slot to exactly one releaser (#664).

        Two parties can end up believing they owe the uploader a slot back:
        ``IndexerWorker.process_file``'s ``finally``, and
        ``WorkerDispatcher.cancel_task`` when ``ray.cancel`` retires the task
        before that body ever runs. Letting both release double-counts; letting
        neither leaks. This actor is single-threaded, so a compare-and-set here
        settles it: the first caller wins, every later one is told to stand
        down.

        Returns ``True`` for a task the actor no longer knows. A slot was
        reserved for *some* task and nothing else will give it back, and this
        branch prefers an undercount (recoverable, and self-heals on the next
        reconciliation) over a leak (permanent, and silently narrows the user's
        quota). In practice it is unreachable: eviction is FIFO by settle time,
        so a task that just settled is the last candidate to be dropped.
        """
        async with self.lock:
            info = self.tasks.get(task_id)
            if info is None:
                return True
            if not info.quota_reserved or info.quota_release_claimed:
                return False
            info.quota_release_claimed = True
            return True

    @ray.method(concurrency_group="set")
    async def set_object_ref(self, task_id: str, object_ref: ray.ObjectRef) -> bool:
        async with self.lock:
            info = self._live_task(task_id)
            if info is None:
                return
            info.object_ref = object_ref
            details = info.details or {}
            if self._file_delete_fenced(partition=details.get("partition"), file_id=details.get("file_id")):
                info.state = "CANCELLED"
                return False
            return info.state in ACTIVE_INDEXING_STATES or info.state in TERMINAL_INDEXING_STATES

    @ray.method(concurrency_group="get")
    async def get_state(self, task_id: str) -> str | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.state if info else None

    @ray.method(concurrency_group="get")
    async def get_error(self, task_id: str) -> str | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.error if info else None

    @ray.method(concurrency_group="get")
    async def get_details(self, task_id: str) -> dict | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.details if info else None

    @ray.method(concurrency_group="get")
    async def get_object_ref(self, task_id: str) -> ray.ObjectRef | None:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.object_ref if info else None

    @ray.method(concurrency_group="get")
    async def get_matching_active_task_refs(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        async with self.lock:
            return self._matching_active_task_refs_locked(partition=partition, file_id=file_id)

    @ray.method(concurrency_group="get")
    async def get_matching_active_task_refs_v2(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        async with self.lock:
            return self._matching_active_task_refs_locked(partition=partition, file_id=file_id)

    def _matching_active_task_refs_locked(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        matches = {}
        for task_id, info in self.tasks.items():
            if info.state not in ACTIVE_INDEXING_STATES:
                continue
            details = info.details or {}
            if not details:
                matches[task_id] = PENDING_TASK_DETAILS
                continue
            if details.get("partition") != partition:
                continue
            if file_id is not None and details.get("file_id") != file_id:
                continue
            matches[task_id] = info.object_ref
        return matches

    @ray.method(concurrency_group="queue_info")
    async def get_all_states(self) -> dict[str, str | None]:
        async with self.lock:
            return {tid: info.state for tid, info in self.tasks.items()}

    @ray.method(concurrency_group="queue_info")
    async def get_all_info(self) -> dict[str, dict]:
        async with self.lock:
            return {
                task_id: {
                    "state": info.state,
                    "error": info.error,
                    "details": info.details,
                }
                for task_id, info in self.tasks.items()
            }

    @ray.method(concurrency_group="queue_info")
    async def get_all_user_info(self, user_id: int) -> dict[str, dict]:
        async with self.lock:
            task_ids = self.user_index.get(user_id, set())
            return {
                tid: {
                    "state": self.tasks[tid].state,
                    "error": self.tasks[tid].error,
                    "details": self.tasks[tid].details,
                }
                for tid in task_ids
                if tid in self.tasks
            }

    @ray.method(concurrency_group="queue_info")
    async def get_pool_info(self) -> dict[str, int]:
        return {
            "pool_size": _POOL_SIZE,
            "max_tasks_per_worker": _MAX_TASKS_PER_WORKER,
            "total_capacity": _POOL_SIZE * _MAX_TASKS_PER_WORKER,
        }

    @ray.method(concurrency_group="queue_info")
    async def get_user_pending_task_count(self, user_id: int) -> int:
        async with self.lock:
            task_ids = self.user_index.get(user_id, set())
            return sum(1 for tid in task_ids if (info := self.tasks.get(tid)) and info.state in ACTIVE_INDEXING_STATES)


__all__ = [
    "ACTIVE_INDEXING_STATES",
    "PENDING_TASK_DETAILS",
    "TERMINAL_INDEXING_STATES",
    "TaskInfo",
    "TaskStateManager",
]
