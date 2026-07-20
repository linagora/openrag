from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from services.workers.task_state import PENDING_TASK_DETAILS, TaskStateManager


def _task_state_manager() -> Any:
    return TaskStateManager.__ray_metadata__.modified_class()


@pytest.mark.asyncio
async def test_cancelled_state_is_not_overwritten_by_worker_transitions() -> None:
    manager = _task_state_manager()
    now = [datetime(2026, 7, 20, 8, 0, tzinfo=UTC)]
    manager._now = lambda: now[0]

    await manager.set_state("task-1", "QUEUED")
    now[0] += timedelta(seconds=3)
    assert await manager.set_cancelled_if_active("task-1") is True

    now[0] += timedelta(seconds=7)
    await manager.set_state("task-1", "SERIALIZING")
    await manager.set_state("task-1", "COMPLETED")

    assert await manager.get_state("task-1") == "CANCELLED"
    info = (await manager.get_all_info())["task-1"]
    assert info["created_at"] == "2026-07-20T08:00:00+00:00"
    assert info["duration_ms"] == 3000


@pytest.mark.asyncio
async def test_task_duration_advances_until_completion_then_stops() -> None:
    manager = _task_state_manager()
    now = [datetime(2026, 7, 20, 8, 0, tzinfo=UTC)]
    manager._now = lambda: now[0]

    await manager.set_state("task-1", "QUEUED")
    assert (await manager.get_all_info())["task-1"]["duration_ms"] == 0

    now[0] += timedelta(seconds=5)
    await manager.set_state("task-1", "SERIALIZING")
    assert (await manager.get_all_info())["task-1"]["duration_ms"] == 5000

    now[0] += timedelta(seconds=7)
    await manager.set_state("task-1", "COMPLETED")
    now[0] += timedelta(minutes=1)

    info = (await manager.get_all_info())["task-1"]
    assert info["created_at"] == "2026-07-20T08:00:00+00:00"
    assert info["duration_ms"] == 12000


@pytest.mark.asyncio
async def test_failed_task_duration_stops_when_failure_is_recorded() -> None:
    manager = _task_state_manager()
    now = [datetime(2026, 7, 20, 8, 0, tzinfo=UTC)]
    manager._now = lambda: now[0]

    await manager.set_state("task-1", "QUEUED")
    now[0] += timedelta(seconds=4)
    assert await manager.set_failed_if_not_cancelled("task-1", "traceback") is True
    now[0] += timedelta(seconds=10)

    assert (await manager.get_all_info())["task-1"]["duration_ms"] == 4000


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
async def test_file_delete_fence_rejects_matching_queued_details() -> None:
    manager = _task_state_manager()
    now = [datetime(2026, 7, 20, 8, 0, tzinfo=UTC)]
    manager._now = lambda: now[0]

    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")
    now[0] += timedelta(seconds=2)
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
    now[0] += timedelta(seconds=5)
    assert (await manager.get_all_info())["task-1"]["duration_ms"] == 0


@pytest.mark.asyncio
async def test_file_delete_fence_rejects_late_object_ref_registration() -> None:
    manager = _task_state_manager()
    now = [datetime(2026, 7, 20, 8, 0, tzinfo=UTC)]
    manager._now = lambda: now[0]

    assert await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=None,
    )
    now[0] += timedelta(seconds=3)
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")

    assert await manager.set_object_ref("task-1", {"ref": object()}) is False
    assert await manager.get_state("task-1") == "CANCELLED"
    now[0] += timedelta(seconds=5)
    assert (await manager.get_all_info())["task-1"]["duration_ms"] == 3000


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
