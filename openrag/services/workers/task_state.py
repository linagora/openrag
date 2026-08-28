from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import ray
from core.models.catalog import (
    TASK_CREATED_AT_METADATA_KEY,
    TASK_FINISHED_AT_METADATA_KEY,
    TERMINAL_TASK_STATES,
    DocumentStatus,
)

ACTIVE_INDEXING_STATES = frozenset({"QUEUED", "SERIALIZING"})
# Legacy indexing states removed from the public state machine in #721. Current
# code never writes them, but an old detached Indexer actor surviving a rolling
# deploy on an external Ray cluster still can. The delete/cancel fencing path
# must keep treating them as in-flight so cleanup never misses such a task and
# lets a stale worker write data after the file/partition is gone. Kept out of
# the public active counts and the DocumentStatus enum on purpose — fencing only.
LEGACY_ACTIVE_INDEXING_STATES = frozenset({"CHUNKING", "INSERTING"})
CANCELLABLE_INDEXING_STATES = ACTIVE_INDEXING_STATES | LEGACY_ACTIVE_INDEXING_STATES
RECOVERABLE_TASK_STATES = CANCELLABLE_INDEXING_STATES | {"CANCELLED"}
TERMINAL_INDEXING_STATES = frozenset({"COMPLETED", "FAILED"})
PENDING_TASK_DETAILS = "__openrag_pending_task_details__"
SUBMITTED_TASK_WITHOUT_REF = "__openrag_submitted_task_without_ref__"
_FENCE_KV_KEY = b"file-delete-fences-v1"
_TASK_STATE_KV_NAMESPACE = "openrag-task-state-manager"
_LEGACY_FENCE_ID = "__legacy__"
_RECOVERABLE_TASK_KV_PREFIX = b"recoverable-task-v1:"
_CANCELLATION_TOMBSTONE_TTL_SECONDS = 24 * 60 * 60
_FILE_DELETE_FENCE_TTL_SECONDS = 2 * 60
_CONTENT_CLAIM_REGISTRATION_GRACE_SECONDS = 60
STALE_REFLESS_TASK_ERROR = (
    "Indexing task never exposed a worker reference within the registration grace period; marking it failed as stale."
)


def _task_state_storage_available() -> bool:
    from ray.experimental.internal_kv import _internal_kv_initialized

    return _internal_kv_initialized() and ray.get_runtime_context().get_actor_id() is not None


def _task_state_kv_namespace() -> bytes:
    ray_namespace = base64.urlsafe_b64encode(ray.get_runtime_context().namespace.encode()).rstrip(b"=")
    return _TASK_STATE_KV_NAMESPACE.encode() + b"-" + ray_namespace


def _normalize_file_delete_fences(
    fences: dict[tuple[str, str], dict[str, int | float]],
    *,
    now: float | None = None,
) -> tuple[dict[tuple[str, str], dict[str, int | float]], bool]:
    timestamp = time.time() if now is None else now
    normalized: dict[tuple[str, str], dict[str, int | float]] = {}
    changed = False
    for key, holders in fences.items():
        active: dict[str, int | float] = {}
        for holder, value in holders.items():
            if holder == _LEGACY_FENCE_ID:
                active[holder] = int(value)
            elif isinstance(value, int) and not isinstance(value, bool):
                # Upgrade the pre-lease format without dropping a deletion that
                # may still be running during a rolling deployment.
                active[holder] = timestamp + _FILE_DELETE_FENCE_TTL_SECONDS
                changed = True
            elif isinstance(value, float) and value > timestamp:
                active[holder] = value
            else:
                changed = True
        if active:
            normalized[key] = active
        elif holders:
            changed = True
    return normalized, changed


def _load_file_delete_fences() -> dict[tuple[str, str], dict[str, int | float]]:
    from ray.experimental.internal_kv import _internal_kv_get

    if not _task_state_storage_available():
        return {}
    payload = _internal_kv_get(_FENCE_KV_KEY, namespace=_task_state_kv_namespace())
    if payload is None:
        return {}
    fences = {(partition, file_id): holders for partition, file_id, holders in json.loads(payload)}
    normalized, changed = _normalize_file_delete_fences(fences)
    if changed:
        _save_file_delete_fences(normalized)
    return normalized


def _save_file_delete_fences(fences: dict[tuple[str, str], dict[str, int | float]]) -> None:
    from ray.experimental.internal_kv import _internal_kv_put

    if not _task_state_storage_available():
        return
    payload = json.dumps(
        [[partition, file_id, holders] for (partition, file_id), holders in sorted(fences.items())],
        separators=(",", ":"),
    )
    _internal_kv_put(_FENCE_KV_KEY, payload, overwrite=True, namespace=_task_state_kv_namespace())


def _recoverable_task_key(task_id: str) -> bytes:
    return _RECOVERABLE_TASK_KV_PREFIX + base64.urlsafe_b64encode(task_id.encode())


def _decode_recoverable_task(payload: bytes) -> tuple[str, TaskInfo, float | None]:
    import ray.cloudpickle as cloudpickle

    record = cloudpickle.loads(payload)
    if len(record) == 2:
        task_id, info = record
        return task_id, info, None
    task_id, info, expires_at = record
    return task_id, info, expires_at


def _load_recoverable_tasks() -> dict[str, TaskInfo]:
    from ray.experimental.internal_kv import _internal_kv_del, _internal_kv_get, _internal_kv_list

    if not _task_state_storage_available():
        return {}
    tasks: dict[str, TaskInfo] = {}
    namespace = _task_state_kv_namespace()
    for key in _internal_kv_list(_RECOVERABLE_TASK_KV_PREFIX, namespace=namespace):
        payload = _internal_kv_get(key, namespace=namespace)
        if payload is None:
            continue
        task_id, info, expires_at = _decode_recoverable_task(payload)
        if expires_at is not None and expires_at <= time.time() and not _cancelled_task_unsettled(info):
            _internal_kv_del(key, namespace=namespace)
            continue
        tasks[task_id] = info
    return tasks


def _recovery_snapshot(info: TaskInfo, *, now: float | None = None) -> tuple[TaskInfo, float | None]:
    if info.state == "FAILED" and info.error == STALE_REFLESS_TASK_ERROR:
        timestamp = time.time() if now is None else now
        return info, timestamp + _CANCELLATION_TOMBSTONE_TTL_SECONDS
    if info.state != "CANCELLED":
        return info, None
    # Keep the worker reference until cancellation is confirmed. If the actor
    # restarts between the durable claim and ray.cancel(), a retry still needs
    # this reference to stop the live worker.
    snapshot = TaskInfo(
        state="CANCELLED",
        details=info.details,
        object_ref=info.object_ref,
        worker_submitted=getattr(info, "worker_submitted", False),
        submission_started_at=getattr(info, "submission_started_at", None),
    )
    if _cancelled_task_unsettled(snapshot):
        return snapshot, None
    timestamp = time.time() if now is None else now
    return snapshot, timestamp + _CANCELLATION_TOMBSTONE_TTL_SECONDS


def _cancelled_task_unsettled(info: TaskInfo) -> bool:
    if info.state != "CANCELLED":
        return False
    object_ref = info.object_ref
    ref = object_ref.get("ref") if isinstance(object_ref, dict) else object_ref
    return ref is not None or getattr(info, "worker_submitted", False)


def _save_recoverable_task(task_id: str, info: TaskInfo) -> None:
    import ray.cloudpickle as cloudpickle
    from ray.experimental.internal_kv import _internal_kv_del, _internal_kv_put

    if not _task_state_storage_available():
        return
    key = _recoverable_task_key(task_id)
    if info.state in RECOVERABLE_TASK_STATES or (info.state == "FAILED" and info.error == STALE_REFLESS_TASK_ERROR):
        snapshot, expires_at = _recovery_snapshot(info)
        _internal_kv_put(
            key,
            cloudpickle.dumps((task_id, snapshot, expires_at)),
            overwrite=True,
            namespace=_task_state_kv_namespace(),
        )
    else:
        _internal_kv_del(key, namespace=_task_state_kv_namespace())


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


@dataclass
class TaskInfo:
    state: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    object_ref: ray.ObjectRef | None = None
    worker_submitted: bool = False
    submission_started_at: float | None = None


def _object_ref_is_ready(object_ref: Any) -> bool:
    ref = object_ref.get("ref") if isinstance(object_ref, dict) else object_ref
    if ref is None:
        return False
    try:
        ready, _ = ray.wait([ref], num_returns=1, timeout=0)
    except Exception:
        # Readiness uncertainty must preserve the claim; reclaiming it could
        # let a second upload run alongside a worker that is still active.
        return False
    return bool(ready)


def _content_claim_registration_expired(details: dict[str, Any]) -> bool:
    metadata = details.get("metadata")
    created_at = metadata.get(TASK_CREATED_AT_METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(created_at, str):
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) >= created + timedelta(seconds=_CONTENT_CLAIM_REGISTRATION_GRACE_SECONDS)


@ray.remote(concurrency_groups={"set": 1000, "get": 1000, "queue_info": 1000})
class TaskStateManager:
    def __init__(self) -> None:
        self.tasks = _load_recoverable_tasks()
        self.user_index: dict[int | None, set[str]] = {}
        for task_id, info in self.tasks.items():
            self.user_index.setdefault(info.details.get("user_id"), set()).add(task_id)
        self.file_delete_fences = _load_file_delete_fences()
        # Ray runs each concurrency group on a separate event loop. A single
        # asyncio lock cannot safely coordinate methods across those loops.
        self.lock = threading.Lock()

    def _ensure_task(self, task_id: str) -> TaskInfo:
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

    def _prune_expired_file_delete_fences(self) -> None:
        updated, changed = _normalize_file_delete_fences(self.file_delete_fences)
        if changed:
            _save_file_delete_fences(updated)
            self.file_delete_fences = updated

    def _file_delete_fenced(self, *, partition: str | None, file_id: str | None) -> bool:
        if partition is None or file_id is None:
            return False
        self._prune_expired_file_delete_fences()
        return bool(self.file_delete_fences.get((partition, file_id)))

    def _expire_refless_task_if_stale_locked(self, task_id: str, info: TaskInfo) -> bool:
        ref = info.object_ref.get("ref") if isinstance(info.object_ref, dict) else info.object_ref
        submission_started_at = getattr(info, "submission_started_at", None)
        submission_pending = isinstance(submission_started_at, (int, float)) and (
            time.time() < submission_started_at + _CONTENT_CLAIM_REGISTRATION_GRACE_SECONDS
        )
        if (
            info.state not in CANCELLABLE_INDEXING_STATES
            or ref is not None
            or getattr(info, "worker_submitted", False)
            or submission_pending
            or not _content_claim_registration_expired(info.details or {})
        ):
            return False
        info.state = "FAILED"
        info.error = STALE_REFLESS_TASK_ERROR
        _save_recoverable_task(task_id, info)
        return True

    @ray.method(concurrency_group="set")
    async def begin_file_delete(self, *, partition: str, file_id: str, fence_id: str | None = None) -> None:
        with self.lock:
            self._prune_expired_file_delete_fences()
            key = (partition, file_id)
            updated = dict(self.file_delete_fences)
            holders = dict(updated.get(key, {}))
            holder = fence_id or _LEGACY_FENCE_ID
            holders[holder] = (
                time.time() + _FILE_DELETE_FENCE_TTL_SECONDS if fence_id else int(holders.get(holder, 0)) + 1
            )
            updated[key] = holders
            _save_file_delete_fences(updated)
            self.file_delete_fences = updated

    @ray.method(concurrency_group="set")
    async def renew_file_delete(self, *, partition: str, file_id: str, fence_id: str) -> bool:
        with self.lock:
            self._prune_expired_file_delete_fences()
            key = (partition, file_id)
            holders = dict(self.file_delete_fences.get(key, {}))
            if fence_id not in holders:
                return False
            holders[fence_id] = time.time() + _FILE_DELETE_FENCE_TTL_SECONDS
            updated = dict(self.file_delete_fences)
            updated[key] = holders
            _save_file_delete_fences(updated)
            self.file_delete_fences = updated
            return True

    @ray.method(concurrency_group="set")
    async def end_file_delete(self, *, partition: str, file_id: str, fence_id: str | None = None) -> None:
        with self.lock:
            self._prune_expired_file_delete_fences()
            key = (partition, file_id)
            updated = dict(self.file_delete_fences)
            holders = dict(updated.get(key, {}))
            holder = fence_id or _LEGACY_FENCE_ID
            remaining = holders.get(holder, 0) - 1
            if fence_id or remaining <= 0:
                holders.pop(holder, None)
            else:
                holders[holder] = remaining
            if holders:
                updated[key] = holders
            else:
                updated.pop(key, None)
            _save_file_delete_fences(updated)
            self.file_delete_fences = updated

    @ray.method(concurrency_group="set")
    async def set_state(self, task_id: str, state: str) -> bool:
        with self.lock:
            info = self._ensure_task(task_id)
            state_is_fenced = info.state == DocumentStatus.CANCELLED or (
                info.state == "FAILED" and info.error == STALE_REFLESS_TASK_ERROR
            )
            if state_is_fenced and state != info.state:
                return False
            info.state = state
            if state == "SERIALIZING":
                info.worker_submitted = True
                info.submission_started_at = None
            _save_recoverable_task(task_id, info)
            return True

    @ray.method(concurrency_group="set")
    async def set_error(self, task_id: str, tb_str: str) -> None:
        with self.lock:
            info = self._ensure_task(task_id)
            info.error = tb_str
            _save_recoverable_task(task_id, info)

    @ray.method(concurrency_group="set")
    async def set_failed_if_not_cancelled(self, task_id: str, tb_str: str) -> bool:
        """Atomically set state to FAILED and record the traceback, unless already CANCELLED."""
        with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state == "CANCELLED":
                return False
            info.state = "FAILED"
            info.error = tb_str
            _save_recoverable_task(task_id, info)
            return True

    @ray.method(concurrency_group="set")
    async def set_cancelled_if_active(self, task_id: str) -> bool:
        with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state in TERMINAL_TASK_STATES:
                return False
            info.state = "CANCELLED"
            _save_recoverable_task(task_id, info)
            return True

    @ray.method(concurrency_group="set")
    async def finish_cancellation(self, task_id: str) -> bool:
        with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state != "CANCELLED":
                return False
            object_ref = info.object_ref
            ref = object_ref.get("ref") if isinstance(object_ref, dict) else object_ref
            if ref is not None and not _object_ref_is_ready(object_ref):
                return False
            info.object_ref = None
            info.worker_submitted = False
            info.submission_started_at = None
            _save_recoverable_task(task_id, info)
            return True

    @ray.method(concurrency_group="set")
    async def finish_rejected_submission(self, task_id: str) -> bool:
        """Clear a submitted-task fence after the pool proves its worker settled."""
        with self.lock:
            info = self.tasks.get(task_id)
            if info is None:
                return False
            info.object_ref = None
            info.worker_submitted = False
            info.submission_started_at = None
            if info.state in CANCELLABLE_INDEXING_STATES:
                info.state = "FAILED"
                info.error = "Indexer worker submission was rejected after the worker settled."
            _save_recoverable_task(task_id, info)
            return True

    @ray.method(concurrency_group="set")
    async def expire_refless_task_if_stale(self, task_id: str) -> bool:
        with self.lock:
            info = self.tasks.get(task_id)
            return info is not None and self._expire_refless_task_if_stale_locked(task_id, info)

    @ray.method(concurrency_group="set")
    async def set_details(
        self,
        task_id: str,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
    ) -> None:
        with self.lock:
            info = self._ensure_task(task_id)
            self._record_details(
                task_id,
                info,
                file_id=file_id,
                partition=partition,
                metadata=metadata,
                user_id=user_id,
            )
            _save_recoverable_task(task_id, info)

    @ray.method(concurrency_group="set")
    async def set_queued_details(
        self,
        task_id: str,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
    ) -> bool:
        with self.lock:
            info = self._ensure_task(task_id)
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
            if self._file_delete_fenced(partition=partition, file_id=file_id):
                info.state = "CANCELLED"
                _save_recoverable_task(task_id, info)
                return False
            info.state = "QUEUED"
            _save_recoverable_task(task_id, info)
            return True

    @ray.method(concurrency_group="set")
    async def begin_worker_submission(self, task_id: str) -> bool:
        with self.lock:
            info = self.tasks.get(task_id)
            if info is None or info.state not in CANCELLABLE_INDEXING_STATES:
                return False
            if self._expire_refless_task_if_stale_locked(task_id, info):
                return False
            info.submission_started_at = time.time()
            _save_recoverable_task(task_id, info)
            return True

    @ray.method(concurrency_group="set")
    async def set_object_ref(self, task_id: str, object_ref: ray.ObjectRef) -> bool:
        with self.lock:
            info = self._ensure_task(task_id)
            if info.state == "FAILED" and info.error == STALE_REFLESS_TASK_ERROR:
                return False
            if self._expire_refless_task_if_stale_locked(task_id, info):
                return False
            info.object_ref = object_ref
            info.worker_submitted = True
            info.submission_started_at = None
            details = info.details or {}
            if self._file_delete_fenced(partition=details.get("partition"), file_id=details.get("file_id")):
                info.state = "CANCELLED"
                _save_recoverable_task(task_id, info)
                return False
            accepted = info.state in ACTIVE_INDEXING_STATES or info.state in TERMINAL_INDEXING_STATES
            _save_recoverable_task(task_id, info)
            return accepted

    @ray.method(concurrency_group="get")
    async def get_state(self, task_id: str) -> str | None:
        with self.lock:
            info = self.tasks.get(task_id)
            return info.state if info else None

    @ray.method(concurrency_group="get")
    async def get_error(self, task_id: str) -> str | None:
        with self.lock:
            info = self.tasks.get(task_id)
            return info.error if info else None

    @ray.method(concurrency_group="get")
    async def get_details(self, task_id: str) -> dict | None:
        with self.lock:
            info = self.tasks.get(task_id)
            return info.details if info else None

    @ray.method(concurrency_group="get")
    async def get_object_ref(self, task_id: str) -> ray.ObjectRef | None:
        with self.lock:
            info = self.tasks.get(task_id)
            return info.object_ref if info else None

    @ray.method(concurrency_group="get")
    async def get_matching_active_task_refs(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        with self.lock:
            return self._matching_active_task_refs_locked(partition=partition, file_id=file_id)

    @ray.method(concurrency_group="get")
    async def get_matching_active_task_refs_v2(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        with self.lock:
            return self._matching_active_task_refs_locked(partition=partition, file_id=file_id)

    @ray.method(concurrency_group="get")
    async def get_content_claim_task_ids(self, *, partition: str) -> set[str]:
        """Return active tasks and cancellations whose workers have not settled."""
        with self.lock:
            matches = set()
            for task_id, info in self.tasks.items():
                worker_submitted = getattr(info, "worker_submitted", False)
                submission_started_at = getattr(info, "submission_started_at", None)
                submission_pending = isinstance(submission_started_at, (int, float)) and (
                    time.time() < submission_started_at + _CONTENT_CLAIM_REGISTRATION_GRACE_SECONDS
                )
                owns_claim = info.state in CANCELLABLE_INDEXING_STATES or (
                    info.state == "CANCELLED" and (info.object_ref is not None or worker_submitted)
                )
                if not owns_claim:
                    continue
                metadata = (info.details or {}).get("metadata")
                has_finished = isinstance(metadata, dict) and TASK_FINISHED_AT_METADATA_KEY in metadata
                if has_finished or _object_ref_is_ready(info.object_ref):
                    continue
                details = info.details or {}
                ref = info.object_ref.get("ref") if isinstance(info.object_ref, dict) else info.object_ref
                if (
                    ref is None
                    and not worker_submitted
                    and not submission_pending
                    and _content_claim_registration_expired(details)
                ):
                    continue
                if details and details.get("partition") != partition:
                    continue
                matches.add(task_id)
            return matches

    def _matching_active_task_refs_locked(
        self,
        *,
        partition: str,
        file_id: str | None = None,
    ) -> dict[str, ray.ObjectRef | None | str]:
        matches = {}
        for task_id, info in self.tasks.items():
            if info.state not in CANCELLABLE_INDEXING_STATES and not _cancelled_task_unsettled(info):
                continue
            details = info.details or {}
            if not details:
                matches[task_id] = PENDING_TASK_DETAILS
                continue
            if details.get("partition") != partition:
                continue
            if file_id is not None and details.get("file_id") != file_id:
                continue
            if info.object_ref is None and getattr(info, "worker_submitted", False):
                matches[task_id] = SUBMITTED_TASK_WITHOUT_REF
            else:
                matches[task_id] = info.object_ref
        return matches

    @ray.method(concurrency_group="queue_info")
    async def get_all_states(self) -> dict[str, str | None]:
        with self.lock:
            return {tid: info.state for tid, info in self.tasks.items()}

    @ray.method(concurrency_group="queue_info")
    async def get_all_info(self) -> dict[str, dict]:
        with self.lock:
            return {
                task_id: {
                    "state": info.state,
                    "error": info.error,
                    "details": info.details,
                    "worker_submitted": getattr(info, "worker_submitted", False),
                }
                for task_id, info in self.tasks.items()
            }

    @ray.method(concurrency_group="queue_info")
    async def get_all_user_info(self, user_id: int) -> dict[str, dict]:
        with self.lock:
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
    async def supports_in_place_restart(self) -> bool:
        """Identify actors created with the restart policy introduced by #841."""
        return True

    @ray.method(concurrency_group="queue_info")
    async def get_user_pending_task_count(self, user_id: int) -> int:
        with self.lock:
            task_ids = self.user_index.get(user_id, set())
            return sum(1 for tid in task_ids if (info := self.tasks.get(tid)) and info.state in ACTIVE_INDEXING_STATES)


__all__ = [
    "ACTIVE_INDEXING_STATES",
    "CANCELLABLE_INDEXING_STATES",
    "LEGACY_ACTIVE_INDEXING_STATES",
    "PENDING_TASK_DETAILS",
    "SUBMITTED_TASK_WITHOUT_REF",
    "STALE_REFLESS_TASK_ERROR",
    "TERMINAL_INDEXING_STATES",
    "TaskInfo",
    "TaskStateManager",
]
