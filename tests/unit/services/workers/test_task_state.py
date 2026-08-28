from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import pytest
import services.workers.task_state as task_state_module
from services.workers.task_state import PENDING_TASK_DETAILS, TaskInfo, TaskStateManager


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
        await manager.set_object_ref(task_id, ref)

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
    assert await manager.get_state("task-1") == "FAILED"
    assert await manager.get_object_ref("task-1") is None


@pytest.mark.asyncio
async def test_submitted_refless_task_keeps_claim_through_cancellation_and_registration() -> None:
    manager = _task_state_manager()
    await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": datetime.now(UTC).isoformat()},
        user_id=None,
    )

    assert await manager.begin_worker_submission("task-1") is True
    assert await manager.set_state("task-1", "SERIALIZING") is True
    await manager.set_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": "2000-01-01T00:00:00+00:00"},
        user_id=None,
    )
    assert await manager.expire_refless_task_if_stale("task-1") is False
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}

    assert await manager.set_cancelled_if_active("task-1") is True
    assert await manager.get_content_claim_task_ids(partition="tenant-a") == {"task-1"}

    worker_ref = {"ref": object()}
    assert await manager.set_object_ref("task-1", worker_ref) is False
    assert await manager.get_object_ref("task-1") == worker_ref


@pytest.mark.asyncio
async def test_unaccepted_submission_fence_expires(monkeypatch) -> None:
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
    await manager.set_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"_openrag_job_created_at": "2000-01-01T00:00:00+00:00"},
        user_id=None,
    )
    now += task_state_module._CONTENT_CLAIM_REGISTRATION_GRACE_SECONDS + 1

    assert await manager.expire_refless_task_if_stale("task-1") is True
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
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")

    assert await manager.set_object_ref("task-1", {"ref": object()}) is False
    assert await manager.get_state("task-1") == "CANCELLED"


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


def test_cancellation_recovery_snapshot_preserves_claim_owner_and_expires() -> None:
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
    assert expires_at == 100.0 + task_state_module._CANCELLATION_TOMBSTONE_TTL_SECONDS


@pytest.mark.asyncio
async def test_submitted_refless_cancellation_keeps_claim_after_reconstruction(monkeypatch) -> None:
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
