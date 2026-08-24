from __future__ import annotations

from typing import Any

import pytest
import services.workers.task_state as task_state_module
from services.workers.task_state import PENDING_TASK_DETAILS, TaskStateManager


def _task_state_manager() -> Any:
    return TaskStateManager.__ray_metadata__.modified_class()


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
