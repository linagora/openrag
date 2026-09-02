from __future__ import annotations

import threading
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
import services.workers.task_state as task_state_module
from services.workers.task_state import (
    PENDING_TASK_DETAILS,
    SUBMITTED_TASK_WITHOUT_REF,
    TaskInfo,
    TaskStateManager,
)


def _task_state_manager() -> Any:
    return TaskStateManager.__ray_metadata__.modified_class()


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

    assert await manager.set_object_ref("completed-task", {"ref": object()}) is True
    assert await manager.set_object_ref("failed-task", {"ref": object()}) is True
    assert await manager.set_object_ref("cancelled-task", {"ref": object()}) is False

    assert await manager.get_state("completed-task") == "COMPLETED"
    assert await manager.get_state("failed-task") == "FAILED"
    assert await manager.get_state("cancelled-task") == "CANCELLED"


@pytest.mark.asyncio
async def test_late_worker_registration_does_not_reopen_a_settled_cancellation(monkeypatch) -> None:
    manager = _task_state_manager()
    worker_ref = object()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.set_object_ref("task-1", {"ref": worker_ref}) is True
    assert await manager.set_cancelled_if_active("task-1") is True
    monkeypatch.setattr(task_state_module.ray, "wait", lambda *_args, **_kwargs: ([worker_ref], []))
    assert await manager.finish_cancellation("task-1") is True
    before = deepcopy(manager.tasks["task-1"])

    assert await manager.set_object_ref("task-1", {"ref": object()}) is False

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
    ref = object()

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
    chunking_ref = {"ref": object()}
    inserting_ref = {"ref": object()}

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

    cancelled_ref = {"ref": object()}
    ready_ref = object()
    await manager.set_object_ref("cancelled-task", cancelled_ref)
    await manager.set_object_ref("ready-active-task", {"ref": ready_ref})
    await manager.set_cancelled_if_active("cancelled-task")
    await manager.set_cancelled_if_active("settled-cancelled-task")
    await manager.set_state("completed-task", "COMPLETED")
    monkeypatch.setattr(
        task_state_module.ray,
        "wait",
        lambda refs, **_kwargs: ([ready_ref], []) if refs == [ready_ref] else ([], refs),
    )

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
    assert await manager.set_object_ref("task-1", {"ref": object()}) is False
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
    worker_ref = {"ref": object()}
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
    worker_ref = {"ref": object()}
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
    worker_ref = object()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert await manager.set_object_ref("task-1", {"ref": worker_ref}) is True
    assert await manager.set_cancelled_if_active("task-1") is True
    monkeypatch.setattr(task_state_module.ray, "wait", lambda refs, **_kwargs: ([], refs))

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

    assert await manager.set_object_ref("task-1", {"ref": object()}) is False
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
    monkeypatch.setattr(task_state_module, "_load_recoverable_tasks", lambda: dict(stored))

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
    task_ref = {"ref": object()}
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
    monkeypatch.setattr(task_state_module, "_load_recoverable_tasks", lambda: dict(stored))

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
        object_ref={"ref": object()},
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

    recovered = task_state_module._load_recoverable_tasks()

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

    assert task_state_module._load_recoverable_tasks() == {}
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

    recovered = task_state_module._load_recoverable_tasks()["task-1"]

    assert getattr(recovered, "cancellation_settlement_expires_at", None) is None


@pytest.mark.asyncio
async def test_submitted_cancellation_keeps_claim_after_reconstruction(monkeypatch) -> None:
    stored: dict[str, TaskInfo] = {}
    monkeypatch.setattr(task_state_module, "_load_recoverable_tasks", lambda: dict(stored))

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
    assert await first_incarnation.set_object_ref("task-1", {"ref": object()}) is True
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
    ref = object()
    manager.tasks["task-1"] = TaskInfo(state="CANCELLED", object_ref={"ref": ref}, worker_submitted=True)

    monkeypatch.setattr(task_state_module.ray, "wait", lambda *_args, **_kwargs: ([ref], []))

    assert await manager.finish_cancellation("task-1") is True

    assert manager.tasks["task-1"].object_ref is None
    assert manager.tasks["task-1"].worker_submitted is False
    assert saved[-1].object_ref is None


@pytest.mark.asyncio
async def test_unsettled_cancellation_keeps_recoverable_worker_reference(monkeypatch) -> None:
    saved: list[TaskInfo] = []
    monkeypatch.setattr(task_state_module, "_save_recoverable_task", lambda _task_id, info: saved.append(info))
    manager = _task_state_manager()
    ref = object()
    manager.tasks["task-1"] = TaskInfo(state="CANCELLED", object_ref={"ref": ref})
    monkeypatch.setattr(task_state_module.ray, "wait", lambda *_args, **_kwargs: ([], [ref]))

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
