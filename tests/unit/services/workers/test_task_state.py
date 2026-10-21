from __future__ import annotations

import threading
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
import ray.exceptions as ray_exceptions
import services.workers.task_state as task_state_module
from core.models.catalog import TASK_FINISHED_AT_METADATA_KEY
from services.workers.task_state import (
    PENDING_TASK_DETAILS,
    SUBMITTED_TASK_WITHOUT_REF,
    TaskInfo,
    TaskStateManager,
)


def _task_state_manager() -> Any:
    return TaskStateManager.__ray_metadata__.modified_class()


class _FakeWorkerRef:
    """A worker ``ObjectRef`` stand-in that ``ray.wait``/``ray.get`` answer for.

    Pending until settled, like a real ref, so a "still running" test holds
    because the ref is pending rather than because Ray rejected the argument.
    """

    def __init__(self) -> None:
        self._outcome: tuple[str, Any] | None = None

    def settle(self, value: Any = None) -> None:
        self._outcome = ("value", value)

    def fail(self, error: BaseException) -> None:
        self._outcome = ("error", error)

    def __deepcopy__(self, _memo: dict) -> _FakeWorkerRef:
        return self


def _owner_died() -> BaseException:
    return ray_exceptions.OwnerDiedError("00" * 28, owner_address=None, call_site="")


@pytest.fixture(autouse=True)
def _fake_worker_refs(monkeypatch) -> None:
    def _require_ref(ref: Any) -> _FakeWorkerRef:
        if not isinstance(ref, _FakeWorkerRef):
            raise TypeError(f"wait() expected a list of ray.ObjectRef, got {type(ref).__name__}")
        return ref

    def wait(refs: list[Any], *, num_returns: int = 1, timeout: float | None = None, **_kwargs: Any):
        ready = [ref for ref in refs if _require_ref(ref)._outcome is not None]
        return ready[:num_returns], [ref for ref in refs if ref not in ready[:num_returns]]

    def get(ref: Any, *, timeout: float | None = None) -> Any:
        outcome = _require_ref(ref)._outcome
        if outcome is None:
            raise ray_exceptions.GetTimeoutError("Get timed out: some object(s) not ready.")
        kind, payload = outcome
        if kind == "error":
            raise payload
        return payload

    monkeypatch.setattr(task_state_module.ray, "wait", wait)
    monkeypatch.setattr(task_state_module.ray, "get", get)


def test_the_task_state_manager_starts_the_ingest_counters(monkeypatch) -> None:
    """The counters it records must exist at 0 before the first document settles."""
    import services.workers.task_state as module

    calls: list[bool] = []
    monkeypatch.setattr(module, "initialize_ingest_counters", lambda: calls.append(True))

    _task_state_manager()

    assert calls == [True]


def test_lock_is_safe_across_ray_concurrency_group_event_loops() -> None:
    manager = _task_state_manager()

    assert isinstance(manager.lock, type(threading.Lock()))


def test_legacy_recoverable_task_record_has_no_expiry() -> None:
    import ray.cloudpickle as cloudpickle

    info = TaskInfo(state="QUEUED")

    assert task_state_module._decode_recoverable_task(cloudpickle.dumps(("task-1", info))) == (
        "task-1",
        info,
        None,
    )


@pytest.mark.asyncio
async def test_reports_support_for_in_place_restart() -> None:
    manager = _task_state_manager()

    assert await manager.supports_in_place_restart() is True


@pytest.mark.asyncio
async def test_reports_support_for_bounded_task_retention() -> None:
    manager = _task_state_manager()

    assert await manager.supports_bounded_task_retention() is True


@pytest.mark.asyncio
async def test_reports_support_for_explicit_completion_outcomes() -> None:
    manager = _task_state_manager()

    assert await manager.supports_explicit_completion_outcomes() is True


@pytest.mark.asyncio
async def test_failure_reason_settles_atomically_with_traceback() -> None:
    manager = _task_state_manager()
    await manager.set_state("task-1", "QUEUED")

    accepted = await manager.set_failed_with_reason_if_not_cancelled(
        "task-1",
        "traceback",
        "RuntimeError: parser failed",
    )

    assert accepted is True
    assert await manager.get_state("task-1") == "FAILED"
    assert await manager.get_error("task-1") == "traceback"
    assert await manager.get_error_reason("task-1") == "RuntimeError: parser failed"


@pytest.mark.asyncio
async def test_failure_reason_does_not_overwrite_cancellation() -> None:
    manager = _task_state_manager()
    await manager.set_state("task-1", "QUEUED")
    await manager.set_cancelled_if_active("task-1")

    accepted = await manager.set_failed_with_reason_if_not_cancelled(
        "task-1",
        "traceback",
        "RuntimeError: late failure",
    )

    assert accepted is False
    assert await manager.get_state("task-1") == "CANCELLED"
    assert await manager.get_error_reason("task-1") is None


@pytest.mark.asyncio
async def test_cancelled_state_is_not_overwritten_by_worker_transitions() -> None:
    manager = _task_state_manager()

    await manager.set_state("task-1", "QUEUED")
    assert await manager.set_cancelled_if_active("task-1") is True

    await manager.set_state("task-1", "SERIALIZING")
    await manager.set_state("task-1", "COMPLETED")

    assert await manager.get_state("task-1") == "CANCELLED"


@pytest.mark.asyncio
async def test_set_object_ref_accepts_terminal_states_without_reopening_task() -> None:
    manager = _task_state_manager()

    await manager.set_state("completed-task", "COMPLETED")
    await manager.set_state("failed-task", "FAILED")
    await manager.set_state("cancelled-task", "CANCELLED")

    assert await manager.set_object_ref("completed-task", {"ref": _FakeWorkerRef()}) is True
    assert await manager.set_object_ref("failed-task", {"ref": _FakeWorkerRef()}) is True
    assert await manager.set_object_ref("cancelled-task", {"ref": _FakeWorkerRef()}) is False

    assert await manager.get_state("completed-task") == "COMPLETED"
    assert await manager.get_state("failed-task") == "FAILED"
    assert await manager.get_state("cancelled-task") == "CANCELLED"


@pytest.mark.asyncio
async def test_late_worker_registration_does_not_reopen_a_settled_cancellation(monkeypatch) -> None:
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.set_object_ref("task-1", {"ref": worker_ref}) is True
    assert await manager.set_cancelled_if_active("task-1") is True
    worker_ref.settle()
    assert await manager.finish_cancellation("task-1") is True
    before = deepcopy(manager.tasks["task-1"])

    assert await manager.set_object_ref("task-1", {"ref": _FakeWorkerRef()}) is False

    assert manager.tasks["task-1"] == before
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == set()


@pytest.mark.asyncio
async def test_set_queued_details_records_active_state_and_routing_together() -> None:
    manager = _task_state_manager()

    accepted = await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"filename": "report.txt"},
        user_id=42,
    )

    assert accepted is True
    assert await manager.get_state("task-1") == "QUEUED"
    assert await manager.get_details("task-1") == {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"filename": "report.txt"},
        "user_id": 42,
    }
    assert "task-1" in await manager.get_all_user_info(42)


@pytest.mark.asyncio
async def test_matching_active_task_refs_treat_detail_less_queued_tasks_as_pending_registration() -> None:
    manager = _task_state_manager()
    ref = _FakeWorkerRef()

    await manager.set_state("queued-without-details", "QUEUED")
    await manager.set_object_ref("queued-without-details", {"ref": ref})
    await manager.set_state("completed-without-details", "COMPLETED")
    await manager.set_state("other-partition", "QUEUED")
    await manager.set_details(
        "other-partition",
        file_id="file-2",
        partition="tenant-b",
        metadata={},
        user_id=1,
    )

    expected = {"queued-without-details": PENDING_TASK_DETAILS}

    assert await manager.get_matching_active_task_refs(partition="tenant-a", file_id="file-1") == expected
    assert await manager.get_matching_active_task_refs_v2(partition="tenant-a", file_id="file-1") == expected


@pytest.mark.asyncio
async def test_matching_active_task_refs_preserve_submitted_tasks_without_refs() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.begin_worker_submission("task-1") is True

    expected = {"task-1": SUBMITTED_TASK_WITHOUT_REF}
    assert await manager.get_matching_active_task_refs_v2(partition="tenant-a", file_id="file-1") == expected

    assert await manager.set_state("task-1", "SERIALIZING") is False
    assert await manager.set_cancelled_if_active("task-1") is True
    assert await manager.get_matching_active_task_refs_v2(partition="tenant-a", file_id="file-1") == {}


@pytest.mark.asyncio
async def test_delete_cleanup_still_fences_legacy_indexing_states() -> None:
    # Regression for #721: CHUNKING/INSERTING are gone from the public state
    # machine, but an old detached Indexer surviving a rolling deploy on an
    # external Ray cluster can still report them. The delete/cancel fencing path
    # must keep matching them, otherwise cleanup misses the in-flight task and the
    # stale worker writes data back after the file is already gone.
    manager = _task_state_manager()
    chunking_ref = {"ref": _FakeWorkerRef()}
    inserting_ref = {"ref": _FakeWorkerRef()}

    for task_id, state, ref in (
        ("chunking-task", "CHUNKING", chunking_ref),
        ("inserting-task", "INSERTING", inserting_ref),
    ):
        await manager.set_details(task_id, file_id="file-1", partition="tenant-a", metadata={}, user_id=1)
        await manager.set_state(task_id, state)
        assert await manager.set_object_ref(task_id, ref) is True

    expected = {"chunking-task": chunking_ref, "inserting-task": inserting_ref}
    assert await manager.get_matching_active_task_refs(partition="tenant-a", file_id="file-1") == expected
    assert await manager.get_matching_active_task_refs_v2(partition="tenant-a", file_id="file-1") == expected


@pytest.mark.asyncio
async def test_content_claim_owners_include_only_unsettled_workers(monkeypatch) -> None:
    manager = _task_state_manager()
    metadata_by_task = {
        "finished-active-task": {"_openrag_job_finished_at": "2026-08-28T08:00:00+00:00"},
        "recent-refless-task": {"_openrag_job_created_at": datetime.now(UTC).isoformat()},
        "stale-refless-task": {"_openrag_job_created_at": "2000-01-01T00:00:00+00:00"},
    }

    for task_id, partition in (
        ("active-task", "tenant-a"),
        ("cancelled-task", "tenant-a"),
        ("settled-cancelled-task", "tenant-a"),
        ("finished-active-task", "tenant-a"),
        ("ready-active-task", "tenant-a"),
        ("recent-refless-task", "tenant-a"),
        ("stale-refless-task", "tenant-a"),
        ("completed-task", "tenant-a"),
        ("other-partition-task", "tenant-b"),
    ):
        await manager.set_queued_details(
            task_id,
            file_id=f"{task_id}-file",
            partition=partition,
            metadata=metadata_by_task.get(task_id, {}),
            user_id=None,
        )

    cancelled_ref = {"ref": _FakeWorkerRef()}
    ready_ref = _FakeWorkerRef()
    await manager.set_object_ref("cancelled-task", cancelled_ref)
    await manager.set_object_ref("ready-active-task", {"ref": ready_ref})
    await manager.set_cancelled_if_active("cancelled-task")
    await manager.set_cancelled_if_active("settled-cancelled-task")
    await manager.set_state("completed-task", "COMPLETED")
    ready_ref.settle()

    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {
        "active-task",
        "cancelled-task",
        "recent-refless-task",
    }


@pytest.mark.asyncio
async def test_stale_refless_task_rejects_late_worker_registration() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": "2000-01-01T00:00:00+00:00"},
        user_id=None,
    )

    assert await manager.expire_refless_task_if_stale("task-1") is True
    assert await manager.set_object_ref("task-1", {"ref": _FakeWorkerRef()}) is False
    assert await manager.set_state("task-1", "SERIALIZING") is False
    assert await manager.get_state("task-1") == "FAILED"
    assert await manager.get_object_ref("task-1") is None


@pytest.mark.asyncio
async def test_pending_count_expires_stale_refless_submission_after_grace(monkeypatch) -> None:
    manager = _task_state_manager()
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.begin_worker_submission("task-1") is True

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_059.0)
    assert await manager.get_user_pending_task_count(42) == 1

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_060.0)
    assert await manager.get_user_pending_task_count(42) == 0
    assert await manager.get_state("task-1") == "FAILED"


@pytest.mark.parametrize(
    "method_name",
    ["get_state", "get_all_states", "get_all_info", "get_all_user_info"],
)
@pytest.mark.asyncio
async def test_queue_views_expire_stale_refless_submissions(monkeypatch, method_name: str) -> None:
    manager = _task_state_manager()
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.begin_worker_submission("task-1") is True

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_060.0)
    method = getattr(manager, method_name)
    if method_name == "get_state":
        state = await method("task-1")
    else:
        result = await method(42) if method_name == "get_all_user_info" else await method()
        task = result["task-1"]
        state = task if method_name == "get_all_states" else task["state"]

    assert state == "FAILED"


@pytest.mark.asyncio
async def test_worker_cannot_enter_serializing_before_ref_registration() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": datetime.now(UTC).isoformat()},
        user_id=None,
    )

    assert await manager.begin_worker_submission("task-1") is True
    worker_ref = {"ref": _FakeWorkerRef()}
    assert await manager.set_state("task-1", "SERIALIZING") is False
    assert await manager.set_object_ref("task-1", worker_ref) is True
    assert await manager.set_state("task-1", "SERIALIZING") is True
    assert await manager.get_object_ref("task-1") == worker_ref


@pytest.mark.asyncio
async def test_submission_fence_persists_until_pool_reports_settlement(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(task_state_module.time, "time", lambda: now)
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": datetime.now(UTC).isoformat()},
        user_id=None,
    )

    assert await manager.begin_worker_submission("task-1") is True
    worker_ref = {"ref": _FakeWorkerRef()}
    assert await manager.set_object_ref("task-1", worker_ref) is True
    assert await manager.set_state("task-1", "SERIALIZING") is True
    await manager.set_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": "2000-01-01T00:00:00+00:00"},
        user_id=None,
    )
    now += task_state_module._CONTENT_CLAIM_REGISTRATION_GRACE_SECONDS + 1

    assert await manager.expire_refless_task_if_stale("task-1") is False
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}

    assert await manager.set_cancelled_if_active("task-1") is True
    assert await manager.has_unsettled_cancelled_worker("task-1") is True
    assert await manager.finish_rejected_submission("task-1") is True
    assert await manager.get_state("task-1") == "CANCELLED"
    assert await manager.has_unsettled_cancelled_worker("task-1") is False
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == set()


@pytest.mark.asyncio
async def test_elapsed_time_does_not_release_unready_worker_fences(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(task_state_module.time, "time", lambda: now)
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.set_object_ref("task-1", {"ref": worker_ref}) is True
    assert await manager.set_cancelled_if_active("task-1") is True

    assert await manager.has_unsettled_cancelled_worker("task-1") is True
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}
    assert await manager.get_matching_active_task_refs_v2(partition="tenant-a", file_id="file-1") == {
        "task-1": {"ref": worker_ref}
    }

    now += task_state_module._CANCELLATION_TOMBSTONE_TTL_SECONDS + 1

    assert await manager.finish_cancellation("task-1") is False
    assert await manager.has_unsettled_cancelled_worker("task-1") is True
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}
    assert await manager.get_matching_active_task_refs_v2(partition="tenant-a", file_id="file-1") == {
        "task-1": {"ref": worker_ref}
    }
    assert await manager.get_object_ref("task-1") == {"ref": worker_ref}
    assert manager.tasks["task-1"].worker_submitted is True


@pytest.mark.asyncio
async def test_unaccepted_submission_fence_expires_after_handoff_grace(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(task_state_module.time, "time", lambda: now)
    manager = _task_state_manager()
    for task_id, file_id in (("claim-task", "file-1"), ("delete-task", "file-2")):
        await manager.set_queued_details(
            task_id,
            file_id=file_id,
            partition="tenant-a",
            metadata={"_openrag_job_created_at": datetime.now(UTC).isoformat()},
            user_id=None,
        )
        assert await manager.begin_worker_submission(task_id) is True
    await manager.set_details(
        "claim-task",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": "2000-01-01T00:00:00+00:00"},
        user_id=None,
    )
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {
        "claim-task",
        "delete-task",
    }
    now += task_state_module._CONTENT_CLAIM_REGISTRATION_GRACE_SECONDS + 1

    assert await manager.expire_refless_task_if_stale("claim-task") is True
    assert await manager.get_state("claim-task") == "FAILED"
    assert (
        await manager.get_matching_active_task_refs_v2(
            partition="tenant-a",
            file_id="file-2",
        )
        == {}
    )
    assert await manager.get_state("delete-task") == "FAILED"
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == set()


@pytest.mark.asyncio
async def test_cancelled_unaccepted_handoff_does_not_keep_content_claim() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": datetime.now(UTC).isoformat()},
        user_id=None,
    )

    assert await manager.begin_worker_submission("task-1") is True
    assert await manager.set_cancelled_if_active("task-1") is True

    assert await manager.has_unsettled_cancelled_worker("task-1") is False
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == set()


@pytest.mark.asyncio
async def test_file_delete_fence_rejects_matching_queued_details() -> None:
    manager = _task_state_manager()

    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")
    accepted = await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"filename": "report.txt"},
        user_id=42,
    )

    assert accepted is False
    assert await manager.get_state("task-1") == "CANCELLED"
    assert await manager.get_details("task-1") == {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"filename": "report.txt"},
        "user_id": 42,
    }


@pytest.mark.asyncio
async def test_file_delete_fence_rejects_late_object_ref_registration() -> None:
    manager = _task_state_manager()

    assert await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=None,
    )
    assert await manager.begin_worker_submission("task-1") is True
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")
    before = deepcopy(manager.tasks["task-1"])

    assert await manager.set_object_ref("task-1", {"ref": _FakeWorkerRef()}) is False
    assert manager.tasks["task-1"] == before


@pytest.mark.asyncio
async def test_file_delete_fence_only_blocks_same_partition_and_file() -> None:
    manager = _task_state_manager()

    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")

    assert await manager.set_queued_details(
        "other-file",
        file_id="file-2",
        partition="tenant-a",
        metadata={},
        user_id=None,
    )
    assert await manager.set_queued_details(
        "other-partition",
        file_id="file-1",
        partition="tenant-b",
        metadata={},
        user_id=None,
    )

    assert await manager.get_state("other-file") == "QUEUED"
    assert await manager.get_state("other-partition") == "QUEUED"


@pytest.mark.asyncio
async def test_file_delete_fence_is_counted_for_overlapping_deletes() -> None:
    manager = _task_state_manager()

    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")
    await manager.end_file_delete(partition="tenant-a", file_id="file-1")

    assert (
        await manager.set_queued_details(
            "task-1",
            file_id="file-1",
            partition="tenant-a",
            metadata={},
            user_id=None,
        )
        is False
    )

    await manager.end_file_delete(partition="tenant-a", file_id="file-1")

    assert (
        await manager.set_queued_details(
            "task-2",
            file_id="file-1",
            partition="tenant-a",
            metadata={},
            user_id=None,
        )
        is True
    )


@pytest.mark.asyncio
async def test_file_delete_fence_survives_actor_reconstruction(monkeypatch) -> None:
    stored: dict[tuple[str, str], dict[str, int]] = {}

    monkeypatch.setattr(task_state_module, "_load_file_delete_fences", lambda: dict(stored))

    def save(fences: dict[tuple[str, str], dict[str, int]]) -> None:
        stored.clear()
        stored.update(fences)

    monkeypatch.setattr(task_state_module, "_save_file_delete_fences", save)

    first_incarnation = _task_state_manager()
    await first_incarnation.begin_file_delete(partition="tenant-a", file_id="file-1", fence_id="delete-1")

    reconstructed = _task_state_manager()
    accepted = await reconstructed.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=None,
    )

    assert accepted is False
    await reconstructed.end_file_delete(partition="tenant-a", file_id="file-1", fence_id="delete-1")
    assert stored == {}


@pytest.mark.asyncio
async def test_file_delete_fence_token_makes_retries_idempotent() -> None:
    manager = _task_state_manager()

    await manager.begin_file_delete(partition="tenant-a", file_id="file-1", fence_id="delete-1")
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1", fence_id="delete-1")
    await manager.end_file_delete(partition="tenant-a", file_id="file-1", fence_id="delete-1")

    assert await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=None,
    )


@pytest.mark.asyncio
async def test_file_delete_fence_lease_expires_and_unblocks_indexing(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(task_state_module.time, "time", lambda: now)
    manager = _task_state_manager()
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1", fence_id="abandoned-delete")

    now += task_state_module._FILE_DELETE_FENCE_TTL_SECONDS + 1

    assert await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=None,
    )


@pytest.mark.asyncio
async def test_file_delete_fence_renewal_extends_lease(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(task_state_module.time, "time", lambda: now)
    manager = _task_state_manager()
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1", fence_id="delete-1")

    now += task_state_module._FILE_DELETE_FENCE_TTL_SECONDS - 1
    assert await manager.renew_file_delete(partition="tenant-a", file_id="file-1", fence_id="delete-1")
    now += 2

    assert (
        await manager.set_queued_details(
            "task-1",
            file_id="file-1",
            partition="tenant-a",
            metadata={},
            user_id=None,
        )
        is False
    )


def test_pre_lease_file_delete_fence_gets_migration_grace_period() -> None:
    fences = {("tenant-a", "file-1"): {"old-delete": 1}}

    normalized, changed = task_state_module._normalize_file_delete_fences(fences, now=100.0)

    assert changed is True
    assert normalized == {
        ("tenant-a", "file-1"): {
            "old-delete": 100.0 + task_state_module._FILE_DELETE_FENCE_TTL_SECONDS,
        }
    }


@pytest.mark.asyncio
async def test_active_task_registry_survives_actor_reconstruction(monkeypatch) -> None:
    stored: dict[str, TaskInfo] = {}
    monkeypatch.setattr(task_state_module, "_load_recoverable_tasks", lambda: (dict(stored), {}))

    def save(task_id: str, info: TaskInfo) -> None:
        if info.state in task_state_module.RECOVERABLE_TASK_STATES:
            stored[task_id] = TaskInfo(
                state=info.state,
                error=info.error,
                details=dict(info.details),
                object_ref=info.object_ref,
            )
        else:
            stored.pop(task_id, None)

    monkeypatch.setattr(task_state_module, "_save_recoverable_task", save)

    first_incarnation = _task_state_manager()
    task_ref = {"ref": _FakeWorkerRef()}
    await first_incarnation.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    await first_incarnation.set_object_ref("task-1", task_ref)

    reconstructed = _task_state_manager()

    assert await reconstructed.get_matching_active_task_refs_v2(partition="tenant-a", file_id="file-1") == {
        "task-1": task_ref
    }
    assert "task-1" in await reconstructed.get_all_user_info(42)

    await reconstructed.set_state("task-1", "COMPLETED")
    assert stored == {}


@pytest.mark.asyncio
async def test_cancellation_tombstone_survives_actor_reconstruction(monkeypatch) -> None:
    stored: dict[str, TaskInfo] = {}
    monkeypatch.setattr(task_state_module, "_load_recoverable_tasks", lambda: (dict(stored), {}))

    def save(task_id: str, info: TaskInfo) -> None:
        if info.state in task_state_module.RECOVERABLE_TASK_STATES:
            stored[task_id] = TaskInfo(
                state=info.state,
                error=info.error,
                details=dict(info.details),
                object_ref=info.object_ref,
            )
        else:
            stored.pop(task_id, None)

    monkeypatch.setattr(task_state_module, "_save_recoverable_task", save)

    first_incarnation = _task_state_manager()
    await first_incarnation.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await first_incarnation.set_cancelled_if_active("task-1") is True

    reconstructed = _task_state_manager()
    await reconstructed.set_state("task-1", "COMPLETED")

    assert await reconstructed.get_state("task-1") == "CANCELLED"
    assert stored["task-1"].state == "CANCELLED"


def test_cancellation_recovery_snapshot_preserves_unsettled_claim_owner_without_expiry() -> None:
    info = TaskInfo(
        state="CANCELLED",
        error="private traceback",
        details={"user_id": 42, "metadata": {"secret": "value"}},
        object_ref={"ref": _FakeWorkerRef()},
        worker_submitted=True,
    )

    snapshot, expires_at = task_state_module._recovery_snapshot(info, now=100.0)

    assert snapshot == TaskInfo(
        state="CANCELLED",
        details=info.details,
        object_ref=info.object_ref,
        worker_submitted=True,
    )
    assert expires_at is None


def test_settled_cancellation_recovery_snapshot_expires() -> None:
    snapshot, expires_at = task_state_module._recovery_snapshot(
        TaskInfo(state="CANCELLED", details={"user_id": 42}),
        now=100.0,
    )

    assert snapshot == TaskInfo(state="CANCELLED", details={"user_id": 42})
    assert expires_at == 100.0 + task_state_module._CANCELLATION_TOMBSTONE_TTL_SECONDS


def test_expired_unsettled_cancellation_is_preserved_during_recovery(monkeypatch) -> None:
    import ray.cloudpickle as cloudpickle
    from ray.experimental import internal_kv

    key = task_state_module._recoverable_task_key("task-1")
    info = TaskInfo(
        state="CANCELLED",
        details={"partition": "tenant-a", "file_id": "file-1"},
        worker_submitted=True,
    )
    payload = cloudpickle.dumps(("task-1", info, 99.0))
    deleted: list[bytes] = []

    monkeypatch.setattr(task_state_module, "_task_state_storage_available", lambda: True)
    monkeypatch.setattr(task_state_module, "_task_state_kv_namespace", lambda: b"test")
    monkeypatch.setattr(task_state_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(internal_kv, "_internal_kv_list", lambda *_args, **_kwargs: [key])
    monkeypatch.setattr(internal_kv, "_internal_kv_get", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(
        internal_kv,
        "_internal_kv_del",
        lambda candidate, **_kwargs: deleted.append(candidate),
    )

    recovered, _expiries = task_state_module._load_recoverable_tasks()

    assert recovered["task-1"].state == "CANCELLED"
    assert recovered["task-1"].worker_submitted is True
    assert deleted == []


def test_expired_settled_cancellation_is_removed_during_recovery(monkeypatch) -> None:
    import ray.cloudpickle as cloudpickle
    from ray.experimental import internal_kv

    key = task_state_module._recoverable_task_key("task-1")
    payload = cloudpickle.dumps(("task-1", TaskInfo(state="CANCELLED"), 99.0))
    deleted: list[bytes] = []

    monkeypatch.setattr(task_state_module, "_task_state_storage_available", lambda: True)
    monkeypatch.setattr(task_state_module, "_task_state_kv_namespace", lambda: b"test")
    monkeypatch.setattr(task_state_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(internal_kv, "_internal_kv_list", lambda *_args, **_kwargs: [key])
    monkeypatch.setattr(internal_kv, "_internal_kv_get", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(
        internal_kv,
        "_internal_kv_del",
        lambda candidate, **_kwargs: deleted.append(candidate),
    )

    assert task_state_module._load_recoverable_tasks() == ({}, {})
    assert deleted == [key]


def test_legacy_unsettled_cancellation_remains_unexpired(monkeypatch) -> None:
    import ray.cloudpickle as cloudpickle
    from ray.experimental import internal_kv

    key = task_state_module._recoverable_task_key("task-1")
    storage = {
        key: cloudpickle.dumps(
            (
                "task-1",
                TaskInfo(
                    state="CANCELLED",
                    details={"partition": "tenant-a", "file_id": "file-1"},
                    worker_submitted=True,
                ),
                None,
            )
        )
    }
    monkeypatch.setattr(task_state_module, "_task_state_storage_available", lambda: True)
    monkeypatch.setattr(task_state_module, "_task_state_kv_namespace", lambda: b"test")
    monkeypatch.setattr(internal_kv, "_internal_kv_list", lambda *_args, **_kwargs: list(storage))
    monkeypatch.setattr(internal_kv, "_internal_kv_get", lambda candidate, **_kwargs: storage.get(candidate))
    monkeypatch.setattr(internal_kv, "_internal_kv_del", lambda candidate, **_kwargs: storage.pop(candidate, None))

    recovered = task_state_module._load_recoverable_tasks()[0]["task-1"]

    assert getattr(recovered, "cancellation_settlement_expires_at", None) is None


@pytest.mark.asyncio
async def test_submitted_cancellation_keeps_claim_after_reconstruction(monkeypatch) -> None:
    stored: dict[str, TaskInfo] = {}
    monkeypatch.setattr(task_state_module, "_load_recoverable_tasks", lambda: (dict(stored), {}))

    def save(task_id: str, info: TaskInfo) -> None:
        if info.state in task_state_module.RECOVERABLE_TASK_STATES:
            stored[task_id] = task_state_module._recovery_snapshot(info)[0]
        else:
            stored.pop(task_id, None)

    monkeypatch.setattr(task_state_module, "_save_recoverable_task", save)
    first_incarnation = _task_state_manager()
    await first_incarnation.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": datetime.now(UTC).isoformat()},
        user_id=42,
    )
    assert await first_incarnation.begin_worker_submission("task-1") is True
    assert await first_incarnation.set_object_ref("task-1", {"ref": _FakeWorkerRef()}) is True
    assert await first_incarnation.set_state("task-1", "SERIALIZING") is True
    assert await first_incarnation.set_cancelled_if_active("task-1") is True

    reconstructed = _task_state_manager()

    assert await reconstructed.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}
    assert (await reconstructed.get_all_info())["task-1"]["worker_submitted"] is True
    assert (await reconstructed.get_details("task-1"))["file_id"] == "file-1"


@pytest.mark.asyncio
async def test_finished_cancellation_drops_recoverable_worker_reference(monkeypatch) -> None:
    saved: list[TaskInfo] = []
    monkeypatch.setattr(task_state_module, "_save_recoverable_task", lambda _task_id, info: saved.append(info))
    manager = _task_state_manager()
    ref = _FakeWorkerRef()
    manager.tasks["task-1"] = TaskInfo(state="CANCELLED", object_ref={"ref": ref}, worker_submitted=True)

    ref.settle()

    assert await manager.finish_cancellation("task-1") is True

    assert manager.tasks["task-1"].object_ref is None
    assert manager.tasks["task-1"].worker_submitted is False
    assert saved[-1].object_ref is None


@pytest.mark.asyncio
async def test_unsettled_cancellation_keeps_recoverable_worker_reference(monkeypatch) -> None:
    saved: list[TaskInfo] = []
    monkeypatch.setattr(task_state_module, "_save_recoverable_task", lambda _task_id, info: saved.append(info))
    manager = _task_state_manager()
    ref = _FakeWorkerRef()
    manager.tasks["task-1"] = TaskInfo(state="CANCELLED", object_ref={"ref": ref})

    assert await manager.finish_cancellation("task-1") is False

    assert manager.tasks["task-1"].object_ref == {"ref": ref}
    assert saved == []


@pytest.mark.asyncio
async def test_ref_less_submitted_cancellation_stays_fenced_until_settlement_is_proven(monkeypatch) -> None:
    saved: list[TaskInfo] = []
    monkeypatch.setattr(task_state_module, "_save_recoverable_task", lambda _task_id, info: saved.append(info))
    manager = _task_state_manager()
    manager.tasks["task-1"] = TaskInfo(
        state="CANCELLED",
        details={"partition": "tenant-a", "file_id": "file-1"},
        worker_submitted=True,
    )

    assert await manager.finish_cancellation("task-1") is False

    assert manager.tasks["task-1"].worker_submitted is True
    assert await manager.has_unsettled_cancelled_worker("task-1") is True
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}
    assert saved == []


@pytest.mark.asyncio
async def test_rejected_submission_is_only_unfenced_after_worker_settlement(monkeypatch) -> None:
    saved: list[TaskInfo] = []
    monkeypatch.setattr(task_state_module, "_save_recoverable_task", lambda _task_id, info: saved.append(info))
    manager = _task_state_manager()
    manager.tasks["task-1"] = TaskInfo(
        state="SERIALIZING",
        details={"partition": "tenant-a", "file_id": "file-1"},
        worker_submitted=True,
    )

    assert await manager.finish_rejected_submission("task-1") is True

    info = manager.tasks["task-1"]
    assert info.state == "FAILED"
    assert info.worker_submitted is False
    assert info.object_ref is None
    assert saved[-1] is info


@pytest.mark.asyncio
async def test_terminal_tasks_are_evicted_once_retention_expires(monkeypatch) -> None:
    # Regression for #660: terminal task records were insert-only, so a
    # detached TaskStateManager grew without bound until the actor OOMed.
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()

    for task_id in ("done-task", "failed-task"):
        await manager.set_queued_details(task_id, file_id=task_id, partition="tenant-a", metadata={}, user_id=7)
    await manager.set_state("done-task", "COMPLETED")
    await manager.set_failed_if_not_cancelled("failed-task", "boom")

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)
    await manager.set_queued_details("new-task", file_id="file-2", partition="tenant-a", metadata={}, user_id=7)

    assert set(manager.tasks) == {"new-task"}
    assert manager.user_index == {7: {"new-task"}}
    assert await manager.get_state("done-task") is None


@pytest.mark.asyncio
async def test_terminal_task_retention_is_capped(monkeypatch) -> None:
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 2)
    manager = _task_state_manager()

    for index in range(5):
        task_id = f"task-{index}"
        await manager.set_queued_details(task_id, file_id=task_id, partition="tenant-a", metadata={}, user_id=None)
        await manager.set_state(task_id, "COMPLETED")

    # Settling sheds history itself, so the cap holds without another caller.
    assert set(manager.tasks) == {"task-3", "task-4"}


@pytest.mark.asyncio
async def test_eviction_keeps_active_tasks_and_unsettled_cancellations(monkeypatch) -> None:
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()

    for task_id in ("active-task", "cancelled-task", "completed-task"):
        await manager.set_queued_details(task_id, file_id=task_id, partition="tenant-a", metadata={}, user_id=None)
    await manager.set_object_ref("cancelled-task", {"ref": _FakeWorkerRef()})
    await manager.set_cancelled_if_active("cancelled-task")
    await manager.set_state("completed-task", "COMPLETED")

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)
    await manager.set_queued_details("new-task", file_id="file-2", partition="tenant-a", metadata={}, user_id=None)

    assert set(manager.tasks) == {"active-task", "cancelled-task", "new-task"}


@pytest.mark.asyncio
async def test_a_settling_burst_sheds_history_with_nothing_else_running(monkeypatch) -> None:
    # Admission-time eviction always runs one settle behind, so a batch that
    # ends the queue has to shed its own history instead of waiting for a
    # caller that may never come.
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 2)
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()
    for index in range(4):
        task_id = f"task-{index}"
        await manager.set_queued_details(task_id, file_id=task_id, partition="tenant-a", metadata={}, user_id=None)
    for index in range(4):
        await manager.set_state(f"task-{index}", "COMPLETED")

    assert set(manager.tasks) == {"task-2", "task-3"}

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)
    await manager.set_queued_details("late-task", file_id="late", partition="tenant-a", metadata={}, user_id=None)
    await manager.set_failed_if_not_cancelled("late-task", "boom")

    assert set(manager.tasks) == {"late-task"}


@pytest.mark.asyncio
async def test_cancellation_fence_outlives_the_receipt_window(monkeypatch) -> None:
    # The receipt answers the reads that follow a settle; the tombstone fences
    # late writers for far longer. Evicting the record on the receipt window
    # took the fence with it, and the durable tombstone with that.
    deleted: list[str] = []
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module, "_CANCELLATION_TOMBSTONE_TTL_SECONDS", 240.0)
    monkeypatch.setattr(task_state_module, "_delete_recoverable_task", deleted.append)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()
    await manager.set_queued_details("cancelled-task", file_id="file-1", partition="tenant-a", metadata={}, user_id=1)
    await manager.set_cancelled_if_active("cancelled-task")

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)

    assert await manager.get_state("cancelled-task") == "CANCELLED"
    assert await manager.set_state("cancelled-task", "COMPLETED") is False
    assert deleted == []

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_241.0)

    assert await manager.get_state("cancelled-task") is None
    assert deleted == ["cancelled-task"]


@pytest.mark.asyncio
async def test_an_expired_receipt_behind_a_fence_is_still_evicted(monkeypatch) -> None:
    # A held fence sits at the head of the ledger for far longer than the
    # receipts queued behind it. It must not stall the sweep for them.
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module, "_CANCELLATION_TOMBSTONE_TTL_SECONDS", 240.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()
    for task_id in ("cancelled-task", "done-task"):
        await manager.set_queued_details(task_id, file_id=task_id, partition="tenant-a", metadata={}, user_id=1)
    await manager.set_cancelled_if_active("cancelled-task")
    await manager.set_state("done-task", "COMPLETED")

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)

    assert await manager.get_all_states() == {"cancelled-task": "CANCELLED"}


@pytest.mark.asyncio
async def test_capacity_pressure_does_not_evict_an_unexpired_cancellation_tombstone(monkeypatch) -> None:
    # A settled cancellation's worker fence clears once the worker goes quiet,
    # but the record itself still has to fence a late write for the full 24h
    # tombstone TTL. Capacity eviction used to ignore that: once the cap was
    # exceeded it forgot the head-of-queue record regardless of type, and a
    # later set_state could recreate the task through _ensure_task with no
    # fence at all.
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 1)
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module, "_CANCELLATION_TOMBSTONE_TTL_SECONDS", 86_400.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()
    await manager.set_queued_details("cancelled-task", file_id="f1", partition="tenant-a", metadata={}, user_id=1)
    assert await manager.set_cancelled_if_active("cancelled-task") is True

    # An unrelated task settles well within the cancellation's 24h fence and
    # pushes the ledger over its 1-record cap.
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_100.0)
    await manager.set_queued_details("done-task", file_id="f2", partition="tenant-a", metadata={}, user_id=2)
    await manager.set_state("done-task", "COMPLETED")

    assert "cancelled-task" in manager.tasks
    assert "done-task" not in manager.tasks
    assert await manager.get_state("cancelled-task") == "CANCELLED"

    # A late write from a worker that never saw the cancellation must still be
    # refused, not silently accepted because the record was forgotten.
    assert await manager.set_state("cancelled-task", "SERIALIZING") is False
    assert await manager.get_state("cancelled-task") == "CANCELLED"


@pytest.mark.asyncio
async def test_stored_task_error_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(task_state_module, "_MAX_TASK_ERROR_CHARS", 64)
    manager = _task_state_manager()

    await manager.set_queued_details("task-1", file_id="file-1", partition="tenant-a", metadata={}, user_id=None)
    await manager.set_failed_if_not_cancelled("task-1", "x" * 100 + "RuntimeError: boom")

    error = await manager.get_error("task-1")
    assert len(error) <= 64
    assert error.endswith("RuntimeError: boom")


@pytest.mark.asyncio
async def test_recovered_terminal_tasks_are_subject_to_retention(monkeypatch) -> None:
    # A settled cancellation recovered from the KV store must age out too,
    # otherwise a restarted actor starts life with unevictable records.
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        task_state_module,
        "_load_recoverable_tasks",
        lambda: (
            {
                "settled-cancelled-task": TaskInfo(state="CANCELLED", details={"user_id": 3}),
                "queued-task": TaskInfo(state="QUEUED", details={"user_id": 3}),
            },
            {},
        ),
    )
    manager = _task_state_manager()

    assert set(manager.terminal_tasks) == {"settled-cancelled-task"}

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)
    await manager.set_queued_details("new-task", file_id="file-1", partition="tenant-a", metadata={}, user_id=3)

    assert set(manager.tasks) == {"queued-task", "new-task"}


@pytest.mark.asyncio
async def test_expired_task_is_evicted_when_its_status_is_polled(monkeypatch) -> None:
    # Eviction must not depend on new work arriving: a queue that goes quiet
    # after its last file still has to forget that file's record.
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()
    await manager.set_queued_details("done-task", file_id="file-1", partition="tenant-a", metadata={}, user_id=1)
    await manager.set_state("done-task", "COMPLETED")

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)

    assert await manager.get_state("done-task") is None
    assert manager.tasks == {}
    assert manager.user_index == {}


@pytest.mark.asyncio
async def test_recovery_preserves_the_original_retention_deadline(monkeypatch) -> None:
    # Restarting the actor must not restart the clock, or a settled record
    # recovered just before its deadline would live for a second full window.
    # The persisted deadline is kept as-is rather than rebuilt from a window.
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(
        task_state_module,
        "_load_recoverable_tasks",
        lambda: (
            {"old-task": TaskInfo(state="CANCELLED", details={"user_id": 3})},
            {"old-task": 1_060.0},
        ),
    )
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_050.0)
    manager = _task_state_manager()

    assert manager.terminal_tasks["old-task"] == 1_060.0

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)
    assert await manager.get_state("old-task") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        lambda manager: manager.get_all_states(),
        lambda manager: manager.get_all_info(),
        lambda manager: manager.get_all_user_info(1),
    ],
    ids=["get_all_states", "get_all_info", "get_all_user_info"],
)
async def test_expired_task_is_evicted_when_the_queue_is_listed(monkeypatch, read) -> None:
    # A read-only workload polls the listing and admits nothing, so these reads
    # have to enforce retention too.
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()
    await manager.set_queued_details("done-task", file_id="file-1", partition="tenant-a", metadata={}, user_id=1)
    await manager.set_state("done-task", "COMPLETED")

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)

    assert await read(manager) == {}
    assert manager.tasks == {}
    assert manager.user_index == {}


@pytest.mark.asyncio
async def test_refused_write_on_an_unknown_id_does_not_leak_a_blank_record(monkeypatch) -> None:
    # A late set_state(..., "SERIALIZING") for an id this actor no longer knows
    # (evicted, or never admitted here) used to leave a blank TaskInfo() behind:
    # _ensure_task creates it, the ref check then refuses the write before
    # anything persists, and a record with state=None never reaches
    # terminal_tasks, so nothing would ever evict it.
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()

    assert await manager.set_state("ghost-task", "SERIALIZING") is False
    assert manager.tasks["ghost-task"].state is None
    assert "ghost-task" in manager.terminal_tasks

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)
    assert await manager.get_state("ghost-task") is None
    assert manager.tasks == {}


@pytest.mark.asyncio
async def test_details_written_on_an_evicted_id_does_not_leak_a_stateless_record(monkeypatch) -> None:
    # TaskCompletionTracker._record_finished_at reads details, then writes them
    # back with a finished-at stamp. If the record expires in between the two
    # calls, set_details recreates it through _ensure_task with details but no
    # state, and that state=None record used to be invisible to the retention
    # ledger forever, surfacing as a task with state=None in every listing.
    monkeypatch.setattr(task_state_module, "_TERMINAL_TASK_RETENTION_SECONDS", 60.0)
    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_000.0)
    manager = _task_state_manager()
    await manager.set_queued_details("done", file_id="f", partition="p", metadata={}, user_id=7)
    await manager.set_state("done", "COMPLETED")
    details = await manager.get_details("done")

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_061.0)
    await manager.set_details("done", file_id="f", partition="p", metadata=details["metadata"], user_id=7)

    assert manager.tasks["done"].state is None
    assert "done" in manager.terminal_tasks

    monkeypatch.setattr(task_state_module.time, "time", lambda: 1_122.0)
    assert await manager.get_all_user_info(7) == {}
    assert manager.tasks == {}


@pytest.mark.asyncio
async def test_degraded_stages_are_added_to_active_task_details() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "degraded-task",
        file_id="f1",
        partition="tenant-a",
        metadata={"filename": "report.pdf"},
        user_id=7,
    )

    accepted = await manager.set_degraded_stages("degraded-task", ["caption", "topic_tag"])

    assert accepted is True
    assert await manager.get_details("degraded-task") == {
        "file_id": "f1",
        "partition": "tenant-a",
        "metadata": {"filename": "report.pdf"},
        "user_id": 7,
        "degraded_stages": ["caption", "topic_tag"],
    }

    await manager.set_details(
        "degraded-task",
        file_id="f1",
        partition="tenant-a",
        metadata={"filename": "report.pdf", "finished": True},
        user_id=7,
    )
    assert (await manager.get_details("degraded-task"))["degraded_stages"] == ["caption", "topic_tag"]


@pytest.mark.asyncio
async def test_degraded_stages_do_not_recreate_an_unknown_task() -> None:
    manager = _task_state_manager()

    accepted = await manager.set_degraded_stages("expired-task", ["caption"])

    assert accepted is False
    assert manager.tasks == {}


@pytest.mark.asyncio
async def test_complete_with_degraded_stages_persists_one_settled_snapshot(monkeypatch) -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "degraded-task",
        file_id="f1",
        partition="tenant-a",
        metadata={"filename": "report.pdf"},
        user_id=7,
    )
    saved: list[TaskInfo] = []
    monkeypatch.setattr(
        task_state_module, "_save_recoverable_task", lambda _task_id, info: saved.append(deepcopy(info))
    )

    outcome = await manager.complete_with_degraded_stages(
        "degraded-task",
        ["topic_tag", "caption", "caption", "provider-secret"],
    )

    assert outcome == "completed"
    assert len(saved) == 1
    assert saved[0].state == "COMPLETED"
    assert saved[0].details["degraded_stages"] == ["caption", "topic_tag"]
    assert await manager.get_state("degraded-task") == "COMPLETED"


@pytest.mark.asyncio
async def test_complete_with_degraded_stages_is_idempotent_for_same_value(monkeypatch) -> None:
    manager = _task_state_manager()
    await manager.set_queued_details("task-1", file_id="f1", partition="tenant-a", metadata={}, user_id=7)
    saved: list[TaskInfo] = []
    monkeypatch.setattr(
        task_state_module, "_save_recoverable_task", lambda _task_id, info: saved.append(deepcopy(info))
    )

    assert await manager.complete_with_degraded_stages("task-1", []) == "completed"
    assert await manager.complete_with_degraded_stages("task-1", []) == "completed"

    assert len(saved) == 1
    assert saved[0].details["degraded_stages"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_state", "expected_outcome"),
    [("CANCELLED", "cancelled"), ("FAILED", "conflict")],
)
async def test_complete_with_degraded_stages_reports_fenced_terminal_state(
    terminal_state: str,
    expected_outcome: str,
) -> None:
    manager = _task_state_manager()
    await manager.set_state("task-1", terminal_state)

    assert await manager.complete_with_degraded_stages("task-1", ["caption"]) == expected_outcome
    assert await manager.get_state("task-1") == terminal_state


@pytest.mark.asyncio
async def test_complete_with_degraded_stages_rejects_conflicting_retry() -> None:
    manager = _task_state_manager()
    await manager.set_state("task-1", "QUEUED")
    assert await manager.complete_with_degraded_stages("task-1", ["caption"]) == "completed"

    assert await manager.complete_with_degraded_stages("task-1", ["topic_tag"]) == "conflict"
    assert (await manager.get_details("task-1"))["degraded_stages"] == ["caption"]


@pytest.mark.asyncio
async def test_complete_with_degraded_stages_does_not_recreate_unknown_task() -> None:
    manager = _task_state_manager()

    assert await manager.complete_with_degraded_stages("expired-task", ["caption"]) == "missing"
    assert manager.tasks == {}


@pytest.mark.asyncio
async def test_admission_fence_refuses_a_second_task_for_a_file_already_indexing() -> None:
    manager = _task_state_manager()
    assert await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )

    refused = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert refused == {"accepted": False, "reason": "file_indexing", "existing_task_id": "task-1"}
    assert await manager.get_state("task-2") is None
    assert await manager.get_state("task-1") == "QUEUED"
    # The refusal leaves no stateless record behind: none to list, none holding
    # a retention slot.
    assert "task-2" not in manager.tasks
    assert "task-2" not in manager.terminal_tasks
    assert set(await manager.get_all_info()) == {"task-1"}


@pytest.mark.asyncio
async def test_admission_fence_keeps_a_record_the_refused_call_did_not_create() -> None:
    """Only the record the refused registration made is dropped, never an earlier one."""
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    await manager.set_details("task-2", file_id="file-1", partition="tenant-a", metadata={}, user_id=42)

    refused = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert refused["reason"] == "file_indexing"
    assert manager.tasks["task-2"].details["file_id"] == "file-1"


@pytest.mark.asyncio
async def test_admission_fence_is_scoped_to_the_same_file_and_partition() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )

    for task_id, file_id, partition in (
        ("task-other-file", "file-2", "tenant-a"),
        ("task-other-partition", "file-1", "tenant-b"),
    ):
        admitted = await manager.set_queued_details_v2(
            task_id,
            file_id=file_id,
            partition=partition,
            metadata={},
            user_id=42,
            reject_if_file_active=True,
        )
        assert admitted["accepted"] is True, task_id
        assert await manager.get_state(task_id) == "QUEUED"


@pytest.mark.asyncio
async def test_admission_fence_is_opt_in_so_a_replace_still_queues() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )

    admitted = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )

    assert admitted["accepted"] is True
    assert await manager.get_state("task-2") == "QUEUED"


@pytest.mark.asyncio
async def test_admission_fence_releases_the_file_once_the_first_task_settles() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    await manager.set_state("task-1", "COMPLETED")

    admitted = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert admitted["accepted"] is True


@pytest.mark.asyncio
async def test_admission_fence_ignores_a_task_that_has_not_registered_its_file() -> None:
    """Refusing work has to under-match: a detail-less task names no file yet."""
    manager = _task_state_manager()
    await manager.set_state("task-1", "QUEUED")

    admitted = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert admitted["accepted"] is True


@pytest.mark.asyncio
async def test_admission_fence_keeps_a_cancelled_task_whose_worker_has_not_settled() -> None:
    """Cancellation does not fence the worker's catalog commit, so its file stays busy."""
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.set_object_ref("task-1", {"ref": _FakeWorkerRef()}) is True
    assert await manager.set_cancelled_if_active("task-1") is True

    refused = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert refused == {"accepted": False, "reason": "file_indexing", "existing_task_id": "task-1"}


@pytest.mark.asyncio
async def test_admission_fence_releases_the_file_once_a_cancelled_worker_settles(monkeypatch) -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    worker_ref = _FakeWorkerRef()
    assert await manager.set_object_ref("task-1", {"ref": worker_ref}) is True
    assert await manager.set_cancelled_if_active("task-1") is True
    worker_ref.fail(ray_exceptions.TaskCancelledError())

    admitted = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert admitted["accepted"] is True


@pytest.mark.asyncio
async def test_admission_fence_releases_the_file_of_a_cancelled_task_without_a_worker() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.set_cancelled_if_active("task-1") is True

    admitted = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert admitted["accepted"] is True


@pytest.mark.asyncio
async def test_admission_fence_still_reports_the_delete_fence_and_cancellation() -> None:
    manager = _task_state_manager()
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")

    deleting = await manager.set_queued_details_v2(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )
    assert deleting == {"accepted": False, "reason": "file_deleting", "existing_task_id": None}

    await manager.set_state("task-2", "QUEUED")
    assert await manager.set_cancelled_if_active("task-2") is True
    cancelled = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-2",
        partition="tenant-b",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )
    assert cancelled == {"accepted": False, "reason": "cancelled", "existing_task_id": None}


@pytest.mark.asyncio
async def test_get_active_indexing_task_for_file_reads_the_fence_without_queueing() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )

    assert await manager.get_active_indexing_task_for_file(partition="tenant-a", file_id="file-1") == "task-1"
    assert await manager.get_active_indexing_task_for_file(partition="tenant-a", file_id="file-2") is None
    assert await manager.get_active_indexing_task_for_file(partition="tenant-b", file_id="file-1") is None
    assert await manager.get_all_states() == {"task-1": "QUEUED"}

    await manager.set_state("task-1", "COMPLETED")
    assert await manager.get_active_indexing_task_for_file(partition="tenant-a", file_id="file-1") is None


async def _serializing_task_for_file_1(manager: Any, worker_ref: object) -> None:
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.set_object_ref("task-1", {"ref": worker_ref}) is True
    assert await manager.set_state("task-1", "SERIALIZING") is True


@pytest.mark.asyncio
async def test_admission_fence_releases_the_file_of_a_task_whose_worker_crashed(monkeypatch) -> None:
    """A dead worker leaves the record SERIALIZING; its ready ref must not hold the file forever."""
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await _serializing_task_for_file_1(manager, worker_ref)
    worker_ref.fail(ray_exceptions.ActorDiedError())

    admitted = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert admitted["accepted"] is True
    assert await manager.get_state("task-1") == "SERIALIZING"
    assert await manager.get_active_indexing_task_for_file(partition="tenant-a", file_id="file-1") == "task-2"


@pytest.mark.asyncio
async def test_admission_fence_releases_the_file_of_a_task_stamped_finished() -> None:
    """A rollout can reload a SERIALIZING record whose worker already finished elsewhere."""
    manager = _task_state_manager()
    await _serializing_task_for_file_1(manager, _FakeWorkerRef())
    await manager.set_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={TASK_FINISHED_AT_METADATA_KEY: "2026-09-25T00:00:00+00:00"},
        user_id=42,
    )

    admitted = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert admitted["accepted"] is True


@pytest.mark.asyncio
async def test_admission_fence_keeps_a_serializing_task_whose_worker_is_running() -> None:
    manager = _task_state_manager()
    await _serializing_task_for_file_1(manager, _FakeWorkerRef())

    refused = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )

    assert refused == {"accepted": False, "reason": "file_indexing", "existing_task_id": "task-1"}


@pytest.mark.asyncio
async def test_admission_fence_does_not_refuse_a_retried_registration_of_the_same_task() -> None:
    """The dispatcher retries set_queued_details_v2 across actor reconstruction."""
    manager = _task_state_manager()
    for _attempt in range(2):
        admitted = await manager.set_queued_details_v2(
            "task-1",
            file_id="file-1",
            partition="tenant-a",
            metadata={},
            user_id=42,
            reject_if_file_active=True,
        )
        assert admitted == {"accepted": True, "reason": None, "existing_task_id": None}

    assert await manager.get_state("task-1") == "QUEUED"


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    current = _Clock()
    monkeypatch.setattr(task_state_module.time, "time", current)
    return current


async def _refuses_a_second_upload_of_file_1(manager: Any) -> bool:
    outcome = await manager.set_queued_details_v2(
        "task-2",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
        reject_if_file_active=True,
    )
    if outcome["accepted"]:
        return False
    assert outcome == {"accepted": False, "reason": "file_indexing", "existing_task_id": "task-1"}
    return True


@pytest.mark.asyncio
async def test_orphaned_ref_keeps_the_file_while_its_worker_renews_the_lease(clock) -> None:
    """The pool actor died, so the ref reads ready, but the worker is still running."""
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await _serializing_task_for_file_1(manager, worker_ref)
    worker_ref.fail(_owner_died())

    for _renewal in range(3):
        clock.now += task_state_module.WORKER_LEASE_RENEW_INTERVAL_SECONDS
        assert await manager.renew_worker_lease("task-1") is True
        assert await manager.has_worker_settled("task-1") is False
        assert await manager.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}
        assert await manager.get_active_indexing_task_for_file(partition="tenant-a", file_id="file-1") == "task-1"

    assert await _refuses_a_second_upload_of_file_1(manager) is True


@pytest.mark.asyncio
async def test_orphaned_ref_releases_the_file_once_a_dead_worker_lets_the_lease_lapse(clock) -> None:
    """Owner and worker both gone: nothing renews the lease, so the fence is bounded by its TTL."""
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await _serializing_task_for_file_1(manager, worker_ref)
    worker_ref.fail(_owner_died())
    clock.now += task_state_module._WORKER_LEASE_TTL_SECONDS - 1
    assert await manager.has_worker_settled("task-1") is False

    clock.now += 1

    assert await manager.has_worker_settled("task-1") is True
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == set()
    assert await _refuses_a_second_upload_of_file_1(manager) is False


@pytest.mark.asyncio
async def test_orphaned_cancellation_finishes_only_once_the_worker_lease_lapses(clock) -> None:
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await _serializing_task_for_file_1(manager, worker_ref)
    assert await manager.set_cancelled_if_active("task-1") is True
    worker_ref.fail(_owner_died())

    assert await manager.finish_cancellation("task-1") is False
    assert await manager.has_unsettled_cancelled_worker("task-1") is True

    clock.now += task_state_module._WORKER_LEASE_TTL_SECONDS

    assert await manager.finish_cancellation("task-1") is True
    assert await manager.has_unsettled_cancelled_worker("task-1") is False


@pytest.mark.asyncio
async def test_a_restarted_manager_grants_recovered_tasks_a_fresh_worker_lease(monkeypatch, clock) -> None:
    """A persisted lease kept ageing while nothing could renew it; it must not release a live worker."""
    worker_ref = _FakeWorkerRef()
    worker_ref.fail(_owner_died())
    recovered = TaskInfo(
        state="SERIALIZING",
        details={"partition": "tenant-a", "file_id": "file-1", "metadata": {}},
        object_ref={"ref": worker_ref},
        worker_submitted=True,
        worker_lease_expires_at=clock.now - 1,
    )
    monkeypatch.setattr(task_state_module, "_load_recoverable_tasks", lambda: ({"task-1": recovered}, {}))

    manager = _task_state_manager()

    assert await manager.has_worker_settled("task-1") is False
    clock.now += task_state_module._WORKER_LEASE_TTL_SECONDS
    assert await manager.has_worker_settled("task-1") is True


@pytest.mark.parametrize(
    "error",
    [
        ray_exceptions.RayTaskError("process_file", "Traceback", ValueError("bad file")),
        ray_exceptions.TaskCancelledError(),
        ray_exceptions.ActorDiedError(),
        ray_exceptions.ObjectLostError("00" * 28, owner_address=None, call_site=""),
        ray_exceptions.ReferenceCountingAssertionError("00" * 28, owner_address=None, call_site=""),
    ],
    ids=["task-error", "cancelled", "worker-died", "object-lost-owner-alive", "ref-counting-assertion"],
)
@pytest.mark.asyncio
async def test_a_ref_that_failed_for_any_other_reason_releases_the_file_at_once(error) -> None:
    """Only a dead owner leaves the worker's fate open; the lease is not consulted otherwise."""
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await _serializing_task_for_file_1(manager, worker_ref)
    assert await manager.renew_worker_lease("task-1") is True
    worker_ref.fail(error)

    assert await manager.has_worker_settled("task-1") is True
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == set()
    assert await _refuses_a_second_upload_of_file_1(manager) is False


@pytest.mark.asyncio
async def test_an_unexpected_failure_reading_a_ready_ref_preserves_the_fence(monkeypatch) -> None:
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await _serializing_task_for_file_1(manager, worker_ref)
    worker_ref.settle({"stored_count": 1})

    def broken_get(_ref: Any, *, timeout: float | None = None) -> Any:
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(task_state_module.ray, "get", broken_get)

    assert await manager.has_worker_settled("task-1") is False
    assert await _refuses_a_second_upload_of_file_1(manager) is True


@pytest.mark.asyncio
async def test_a_value_that_is_not_a_ref_preserves_the_fence() -> None:
    """Ray rejects a non-ref outright; uncertainty must never release a file."""
    manager = _task_state_manager()
    await _serializing_task_for_file_1(manager, object())

    assert await manager.has_worker_settled("task-1") is False
    assert await _refuses_a_second_upload_of_file_1(manager) is True


@pytest.mark.asyncio
async def test_worker_lease_renewal_and_settlement_for_unknown_or_refless_tasks() -> None:
    """A missing ref reads the way the fence reads it: settled only once nothing can start a worker."""
    manager = _task_state_manager()

    assert await manager.renew_worker_lease("missing") is None
    assert await manager.has_worker_settled("missing") is True

    await manager.set_queued_details("task-1", file_id="file-1", partition="tenant-a", metadata={}, user_id=42)
    assert await manager.has_worker_settled("task-1") is False
    assert await manager.get_active_indexing_task_for_file(partition="tenant-a", file_id="file-1") == "task-1"

    await manager.set_state("task-1", "FAILED")
    assert await manager.has_worker_settled("task-1") is True


@pytest.mark.asyncio
async def test_a_cancellation_cleared_after_its_worker_settled_reads_settled() -> None:
    manager = _task_state_manager()
    worker_ref = _FakeWorkerRef()
    await _serializing_task_for_file_1(manager, worker_ref)
    assert await manager.set_cancelled_if_active("task-1") is True
    assert await manager.has_worker_settled("task-1") is False

    worker_ref.fail(ray_exceptions.TaskCancelledError())
    assert await manager.finish_cancellation("task-1") is True

    assert await manager.get_object_ref("task-1") is None
    assert await manager.has_worker_settled("task-1") is True


class _WorkerActorTable:
    """Stands in for the GCS actor table the queued-orphan rule reads."""

    def __init__(self) -> None:
        self.alive = True
        self.restarts = 0
        self.error: Exception | None = None
        self.reads: list[str] = []

    def __call__(self, actor_id: str) -> tuple[bool, int] | None:
        self.reads.append(actor_id)
        if self.error is not None:
            return None
        return self.alive, self.restarts


@pytest.fixture
def actor_table(monkeypatch) -> _WorkerActorTable:
    table = _WorkerActorTable()
    monkeypatch.setattr(task_state_module, "read_worker_actor_state", table)
    monkeypatch.setattr(task_state_module, "_worker_actor_states", {})
    return table


async def _orphaned_queued_task_for_file_1(
    manager: Any, *, restarts_at_submission: int | None = None
) -> _FakeWorkerRef:
    """task-1 sent to worker actor ``w1``, never started, and its owner died."""
    worker_ref = _FakeWorkerRef()
    await manager.set_queued_details("task-1", file_id="file-1", partition="tenant-a", metadata={}, user_id=42)
    registration: dict[str, Any] = {"ref": worker_ref, task_state_module.WORKER_ACTOR_ID_KEY: "w1"}
    if restarts_at_submission is not None:
        registration[task_state_module.WORKER_RESTARTS_KEY] = restarts_at_submission
    assert await manager.set_object_ref("task-1", registration) is True
    worker_ref.fail(_owner_died())
    return worker_ref


@pytest.mark.asyncio
async def test_a_queued_orphan_holds_the_file_past_the_lease_while_its_worker_actor_lives(clock, actor_table) -> None:
    """Nothing renews a lease for a task waiting in the actor's queue, so the lease cannot settle it."""
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager)

    clock.now += 10 * task_state_module._WORKER_LEASE_TTL_SECONDS

    assert await manager.has_worker_settled("task-1") is False
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}
    assert await _refuses_a_second_upload_of_file_1(manager) is True
    assert set(actor_table.reads) == {"w1"}


@pytest.mark.asyncio
async def test_a_queued_orphan_settles_once_its_worker_actor_died(clock, actor_table) -> None:
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager)
    assert await manager.has_worker_settled("task-1") is False

    actor_table.alive = False
    task_state_module._worker_actor_states.clear()

    assert await manager.has_worker_settled("task-1") is True
    assert await _refuses_a_second_upload_of_file_1(manager) is False


@pytest.mark.asyncio
async def test_a_queued_orphan_settles_once_its_worker_actor_restarted(clock, actor_table) -> None:
    """A restart drops the queue of the incarnation the dead owner sent the task to."""
    actor_table.restarts = 2
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager)
    assert await manager.has_worker_settled("task-1") is False

    actor_table.restarts = 3
    task_state_module._worker_actor_states.clear()

    assert await manager.has_worker_settled("task-1") is True


@pytest.mark.asyncio
async def test_a_queued_orphan_counts_restarts_from_when_the_pool_sent_it(clock, actor_table) -> None:
    """The worker can restart before anything looks at the orphan, e.g. when a node takes both actors down."""
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager, restarts_at_submission=0)
    actor_table.restarts = 1

    assert await manager.has_worker_settled("task-1") is True


@pytest.mark.asyncio
async def test_a_queued_orphan_without_a_registered_count_takes_it_from_a_fresh_read(clock, actor_table) -> None:
    """A cached read may predate the owner's death, and a restart the queued task survived."""
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager)
    task_state_module._worker_actor_states["w1"] = (task_state_module.time.monotonic(), (True, 0))
    actor_table.restarts = 1

    assert await manager.has_worker_settled("task-1") is False
    assert actor_table.reads == ["w1"]
    assert manager.tasks["task-1"].worker_restarts_baseline == 1


@pytest.mark.asyncio
async def test_a_restart_baseline_from_a_fresh_read_survives_a_manager_restart(clock, actor_table, monkeypatch) -> None:
    """Recovered without it, the next read would stand in and miss a restart that already dropped the task."""
    saved: list[int | None] = []
    monkeypatch.setattr(
        task_state_module,
        "_save_recoverable_task",
        lambda task_id, info: saved.append(info.worker_restarts_baseline),
    )
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager)
    actor_table.restarts = 2

    assert await manager.has_worker_settled("task-1") is False

    assert saved[-1] == 2


def test_cancellation_recovery_snapshot_keeps_the_worker_restart_baseline() -> None:
    info = TaskInfo(state="CANCELLED", object_ref={"ref": object()}, worker_restarts_baseline=4)

    snapshot, _ = task_state_module._recovery_snapshot(info, now=100.0)

    assert snapshot.worker_restarts_baseline == 4


@pytest.mark.asyncio
async def test_a_queued_orphan_holds_while_the_gcs_cannot_be_read(clock, actor_table) -> None:
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager)
    actor_table.error = RuntimeError("GCS unavailable")

    assert await manager.has_worker_settled("task-1") is False
    assert await _refuses_a_second_upload_of_file_1(manager) is True


@pytest.mark.asyncio
async def test_a_started_orphan_follows_its_lease_and_settles_when_the_worker_returns(clock, actor_table) -> None:
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager)

    assert await manager.renew_worker_lease("task-1") is True
    clock.now += task_state_module._WORKER_LEASE_TTL_SECONDS - 1
    assert await manager.has_worker_settled("task-1") is False

    await manager.end_worker_lease("task-1")

    assert await manager.has_worker_settled("task-1") is True
    assert await _refuses_a_second_upload_of_file_1(manager) is False
    assert actor_table.reads == []


@pytest.mark.asyncio
async def test_a_started_orphan_settles_once_its_dead_worker_lets_the_lease_lapse(clock, actor_table) -> None:
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager)
    assert await manager.renew_worker_lease("task-1") is True

    clock.now += task_state_module._WORKER_LEASE_TTL_SECONDS

    assert await manager.has_worker_settled("task-1") is True


@pytest.mark.asyncio
async def test_lease_renewal_tells_a_cancelled_worker_to_stop() -> None:
    manager = _task_state_manager()
    await _serializing_task_for_file_1(manager, _FakeWorkerRef())
    assert await manager.renew_worker_lease("task-1") is True

    assert await manager.set_cancelled_if_active("task-1") is True

    assert await manager.renew_worker_lease("task-1") is False
    assert manager.tasks["task-1"].worker_started is True


@pytest.mark.asyncio
async def test_a_restarted_manager_keeps_what_the_worker_reported(monkeypatch) -> None:
    """Without the started flag, a recovered orphan would wait on its worker actor instead of its lease."""
    saved: dict[str, TaskInfo] = {}
    monkeypatch.setattr(
        task_state_module, "_save_recoverable_task", lambda task_id, info: saved.update({task_id: info})
    )
    manager = _task_state_manager()
    await _serializing_task_for_file_1(manager, _FakeWorkerRef())
    assert await manager.renew_worker_lease("task-1") is True
    assert saved["task-1"].worker_started is True
    await manager.end_worker_lease("task-1")
    assert saved["task-1"].worker_finished is True

    assert await manager.set_cancelled_if_active("task-1") is True
    snapshot, _ = task_state_module._recovery_snapshot(manager.tasks["task-1"])

    assert snapshot.worker_started is True
    assert snapshot.worker_finished is True


@pytest.mark.asyncio
async def test_the_lease_ttl_is_a_constructor_argument(clock) -> None:
    manager = TaskStateManager.__ray_metadata__.modified_class(worker_lease_ttl_seconds=2.0)
    worker_ref = _FakeWorkerRef()
    await _serializing_task_for_file_1(manager, worker_ref)
    worker_ref.fail(_owner_died())
    assert await manager.renew_worker_lease("task-1") is True

    clock.now += 1.5
    assert await manager.has_worker_settled("task-1") is False
    clock.now += 0.5
    assert await manager.has_worker_settled("task-1") is True


@pytest.mark.asyncio
async def test_a_queued_orphan_settled_by_a_restart_is_refused_at_pickup(clock, actor_table) -> None:
    """The count read at submission can already be the incarnation the task was delivered to.

    Settling then releases the file while the task may still run, so the record
    is failed and the worker's pickup is refused rather than indexing late.
    """
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager, restarts_at_submission=0)
    actor_table.restarts = 1

    assert await manager.get_active_indexing_task_for_file(partition="tenant-a", file_id="file-1") is None

    assert await manager.get_state("task-1") == "FAILED"
    assert await manager.get_error("task-1") == task_state_module.ABANDONED_QUEUED_TASK_ERROR
    assert await manager.renew_worker_lease("task-1") is False
    assert await manager.set_state("task-1", "SERIALIZING") is False
    assert await manager.has_worker_settled("task-1") is True
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == set()
    assert await _refuses_a_second_upload_of_file_1(manager) is False


@pytest.mark.asyncio
async def test_a_queued_orphan_settled_by_has_worker_settled_is_refused_at_pickup(clock, actor_table) -> None:
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager, restarts_at_submission=0)
    actor_table.alive = False

    assert await manager.has_worker_settled("task-1") is True

    assert await manager.get_state("task-1") == "FAILED"
    assert await manager.renew_worker_lease("task-1") is False


@pytest.mark.asyncio
async def test_an_abandoned_queued_orphan_stays_refused_after_a_manager_restart(
    monkeypatch, clock, actor_table
) -> None:
    saved: dict[str, TaskInfo] = {}
    monkeypatch.setattr(
        task_state_module, "_save_recoverable_task", lambda task_id, info: saved.update({task_id: info})
    )
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager, restarts_at_submission=0)
    actor_table.restarts = 1
    assert await manager.has_worker_settled("task-1") is True
    monkeypatch.setattr(task_state_module, "_load_recoverable_tasks", lambda: (dict(saved), {}))

    restarted = _task_state_manager()

    assert await restarted.renew_worker_lease("task-1") is False


@pytest.mark.asyncio
async def test_a_cancelled_queued_orphan_settled_by_a_restart_stays_cancelled(clock, actor_table) -> None:
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager, restarts_at_submission=0)
    assert await manager.set_cancelled_if_active("task-1") is True
    actor_table.restarts = 1

    assert await manager.has_worker_settled("task-1") is True

    assert await manager.get_state("task-1") == "CANCELLED"
    assert await manager.renew_worker_lease("task-1") is False


@pytest.mark.asyncio
async def test_a_queued_orphan_picked_up_before_the_restart_is_read_keeps_running(clock, actor_table) -> None:
    """Pickup and settlement are serialized: once the worker started, only its lease decides."""
    manager = _task_state_manager()
    await _orphaned_queued_task_for_file_1(manager, restarts_at_submission=0)
    assert await manager.renew_worker_lease("task-1") is True
    actor_table.restarts = 1

    assert await manager.has_worker_settled("task-1") is False
    assert await manager.get_state("task-1") == "QUEUED"
    assert await manager.renew_worker_lease("task-1") is True
