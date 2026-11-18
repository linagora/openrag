from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from services.workers import worker_lease as worker_lease_module
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
async def test_first_stage_snapshot_is_retried_before_normal_lease_interval(monkeypatch) -> None:
    monkeypatch.setattr(worker_lease_module, "WORKER_LEASE_RENEW_INTERVAL_SECONDS", 0.5)
    monkeypatch.setattr(worker_lease_module, "_INITIAL_STAGE_SNAPSHOT_RETRY_SECONDS", 0.01, raising=False)
    tracker = StageTracker("task-startup")
    calls = []
    snapshot_sent = asyncio.Event()

    def snapshot_provider():
        if calls:
            tracker.start_stage("parse", 0)
            return tracker.snapshot()
        return None

    async def renew(_task_id, snapshot):
        calls.append(snapshot)
        if snapshot is not None:
            snapshot_sent.set()
        return True

    tsm = SimpleNamespace(renew_worker_lease_with_stage_snapshot=SimpleNamespace(remote=renew))
    worker_task = asyncio.create_task(asyncio.sleep(10))
    lease = asyncio.create_task(
        keep_worker_lease(
            tsm,
            "task-startup",
            worker_task=worker_task,
            logger=_QuietLogger(),
            renew_interval=0.5,
            lease_ttl=2.0,
            snapshot_provider=snapshot_provider,
        )
    )

    try:
        await asyncio.wait_for(snapshot_sent.wait(), timeout=0.15)
        assert calls[0] is None
        assert calls[1].summary.current_stage_id == "parse"
    finally:
        lease.cancel()
        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lease
        with pytest.raises(asyncio.CancelledError):
            await worker_task


@pytest.mark.asyncio
async def test_missing_stage_snapshot_retry_backs_off_to_normal_lease_interval(monkeypatch) -> None:
    monkeypatch.setattr(worker_lease_module, "WORKER_LEASE_RENEW_INTERVAL_SECONDS", 0.08)
    monkeypatch.setattr(worker_lease_module, "_INITIAL_STAGE_SNAPSHOT_RETRY_SECONDS", 0.01)
    renewal_times = []
    six_renewals = asyncio.Event()

    async def renew(_task_id, _snapshot):
        renewal_times.append(asyncio.get_running_loop().time())
        if len(renewal_times) == 6:
            six_renewals.set()
        return True

    tsm = SimpleNamespace(renew_worker_lease_with_stage_snapshot=SimpleNamespace(remote=renew))
    worker_task = asyncio.create_task(asyncio.sleep(10))
    lease = asyncio.create_task(
        keep_worker_lease(
            tsm,
            "task-no-snapshot",
            worker_task=worker_task,
            logger=_QuietLogger(),
            renew_interval=0.08,
            lease_ttl=1.0,
            snapshot_provider=lambda: None,
        )
    )

    try:
        await asyncio.wait_for(six_renewals.wait(), timeout=0.5)
        assert renewal_times[1] - renewal_times[0] < 0.06
        assert renewal_times[-1] - renewal_times[-2] >= 0.06
    finally:
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


@pytest.mark.asyncio
async def test_legacy_task_state_manager_sends_snapshots_to_sidecar() -> None:
    tracker = StageTracker("task-legacy")
    tracker.start_stage("parse", 0)
    lease_calls = []
    snapshot_calls = []

    async def renew(task_id):
        lease_calls.append(task_id)
        return True

    async def publish(task_id, snapshot):
        snapshot_calls.append((task_id, snapshot))
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
            snapshot_provider=tracker.snapshot,
            stage_progress_sink=publish,
        )
    )

    for _ in range(20):
        if snapshot_calls:
            break
        await asyncio.sleep(0.005)

    assert lease_calls
    assert snapshot_calls[0][0] == "task-legacy"
    assert snapshot_calls[0][1].summary.current_stage_id == "parse"
    assert snapshot_calls[0][1].event_tail[0].status.value == "started"

    lease.cancel()
    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lease
    with pytest.raises(asyncio.CancelledError):
        await worker_task


@pytest.mark.asyncio
async def test_slow_sidecar_delivery_does_not_delay_worker_lease_renewal() -> None:
    tracker = StageTracker("task-legacy")
    tracker.start_stage("parse", 0)
    lease_calls = []
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()

    async def renew(task_id):
        lease_calls.append(task_id)
        return True

    async def publish(_task_id, _snapshot):
        delivery_started.set()
        await release_delivery.wait()

    tsm = SimpleNamespace(renew_worker_lease=SimpleNamespace(remote=renew))
    worker_task = asyncio.create_task(asyncio.sleep(10))
    lease = asyncio.create_task(
        keep_worker_lease(
            tsm,
            "task-legacy",
            worker_task=worker_task,
            logger=_QuietLogger(),
            renew_interval=0.01,
            lease_ttl=0.2,
            snapshot_provider=tracker.snapshot,
            stage_progress_sink=publish,
        )
    )

    await asyncio.wait_for(delivery_started.wait(), timeout=0.05)
    await asyncio.sleep(0.035)

    assert len(lease_calls) >= 2

    release_delivery.set()
    lease.cancel()
    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await lease
    with pytest.raises(asyncio.CancelledError):
        await worker_task
