from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from services.workers.stage_tracker import StageTracker
from services.workers.worker_lease import keep_worker_lease


class _QuietLogger:
    def warning(self, *_args, **_kwargs) -> None:
        pass


@pytest.mark.asyncio
async def test_stage_snapshot_is_carried_on_worker_lease_renewals() -> None:
    tracker = StageTracker("task-1")
    tracker.start_stage("parse", 0)
    calls = []

    async def renew(task_id, snapshot):
        calls.append((task_id, snapshot))
        return True

    tsm = SimpleNamespace(
        renew_worker_lease=SimpleNamespace(remote=lambda *_args: pytest.fail("legacy lease called")),
        renew_worker_lease_with_stage_snapshot=SimpleNamespace(remote=renew),
    )
    worker_task = asyncio.create_task(asyncio.sleep(10))
    lease = asyncio.create_task(
        keep_worker_lease(
            tsm,
            "task-1",
            worker_task=worker_task,
            logger=_QuietLogger(),
            renew_interval=0.01,
            lease_ttl=0.05,
            snapshot_provider=tracker.snapshot,
        )
    )
    # A separate signal lets the loop complete its first renewal before we stop it.
    await asyncio.sleep(0.02)
    assert calls
    assert calls[0][0] == "task-1"
    assert calls[0][1].summary.current_stage_id == "parse"
    assert calls[0][1].event_tail[0].status.value == "started"

    lease.cancel()
    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lease
    with pytest.raises(asyncio.CancelledError):
        await worker_task


@pytest.mark.asyncio
async def test_snapshot_lease_falls_back_to_existing_lease_method() -> None:
    calls = []

    async def renew(task_id):
        calls.append(task_id)
        return True

    tsm = SimpleNamespace(renew_worker_lease=SimpleNamespace(remote=renew))
    worker_task = asyncio.create_task(asyncio.sleep(10))
    lease = asyncio.create_task(
        keep_worker_lease(
            tsm,
            "task-legacy",
            worker_task=worker_task,
            logger=_QuietLogger(),
            renew_interval=0.01,
            lease_ttl=0.05,
            snapshot_provider=lambda: None,
        )
    )
    await asyncio.sleep(0.02)
    assert calls
    assert set(calls) == {"task-legacy"}

    lease.cancel()
    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lease
    with pytest.raises(asyncio.CancelledError):
        await worker_task
