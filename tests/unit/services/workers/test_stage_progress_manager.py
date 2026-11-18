from __future__ import annotations

import pytest
import services.workers.stage_progress_manager as progress_manager
from services.workers.stage_progress_manager import TaskStageProgressManager
from services.workers.stage_tracker import StageTracker


def _manager(*, ttl_seconds: float = 1.0, max_snapshots: int = 10):
    return TaskStageProgressManager.__ray_metadata__.modified_class(
        ttl_seconds=ttl_seconds,
        max_snapshots=max_snapshots,
    )


@pytest.mark.asyncio
async def test_sidecar_returns_compact_live_snapshots() -> None:
    tracker = StageTracker("task-1")
    tracker.start_stage("parse", 0)
    manager = _manager()

    assert await manager.set_snapshot("task-1", tracker.snapshot()) is True

    result = await manager.get_stage_progress_snapshots(["task-1", "missing"])

    assert list(result) == ["task-1"]
    assert result["task-1"].summary.current_stage_id == "parse"
    assert result["task-1"].event_tail == []


@pytest.mark.asyncio
async def test_sidecar_rejects_stale_and_wrong_task_snapshots() -> None:
    newer_tracker = StageTracker("task-1")
    newer_tracker.start_stage("parse", 0)
    newer_tracker.set_substage("pymupdf", "PyMuPDF", progress=1, total=2)
    newer = newer_tracker.snapshot()
    manager = _manager()

    assert await manager.set_snapshot("task-1", newer) is True
    assert await manager.set_snapshot("task-1", StageTracker("task-1").snapshot()) is False
    assert await manager.set_snapshot("other-task", newer) is False
    result = await manager.get_stage_progress_snapshots(["task-1"])

    assert result["task-1"].summary.snapshot_revision == newer.summary.snapshot_revision


@pytest.mark.asyncio
async def test_sidecar_expires_snapshots_after_worker_heartbeats_stop(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(progress_manager, "monotonic", lambda: now[0])
    tracker = StageTracker("task-1")
    tracker.start_stage("parse", 0)
    manager = _manager(ttl_seconds=5.0)

    assert await manager.set_snapshot("task-1", tracker.snapshot()) is True
    now[0] += 5.0

    assert await manager.get_stage_progress_snapshots(["task-1"]) == {}


@pytest.mark.asyncio
async def test_sidecar_capacity_is_bounded_and_can_remove_a_task() -> None:
    first = StageTracker("task-1")
    first.start_stage("parse", 0)
    second = StageTracker("task-2")
    second.start_stage("embed", 5)
    manager = _manager(max_snapshots=1)

    await manager.set_snapshot("task-1", first.snapshot())
    await manager.set_snapshot("task-2", second.snapshot())
    snapshots = await manager.get_stage_progress_snapshots(["task-1", "task-2"])
    assert set(snapshots) == {"task-2"}
    assert snapshots["task-2"].summary.current_stage_id == "embed"

    await manager.remove_snapshot("task-2")
    assert await manager.get_stage_progress_snapshots(["task-2"]) == {}
