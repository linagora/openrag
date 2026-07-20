from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from services.workers.task_state import TaskStateManager


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
