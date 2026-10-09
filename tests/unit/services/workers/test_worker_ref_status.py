"""``_object_ref_status`` against real Ray refs, including one whose owner died.

The TaskStateManager tests drive the fence through a fake ref; these pin the
Ray behaviour that fake mirrors. The worker ref is owned by the pool actor that
submitted the task, so killing that actor while the worker runs is exactly the
case the review found: ``ray.wait`` reports the ref ready, and only
``ray.get`` raising ``OwnerDiedError`` tells it apart from a settled worker.

The last tests run the whole rule on a real TaskStateManager: a worker actor
that runs one task at a time, a pool stand-in that owns the refs, and a task
still queued on the worker when the pool dies.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import ray
import ray.cloudpickle
import services.workers.task_state as task_state_module
from services.workers.task_state import (
    _REF_OWNER_DIED,
    _REF_PENDING,
    _REF_SETTLED,
    TaskStateManager,
    _object_ref_status,
)

_SETTLE_TIMEOUT_SECONDS = 30.0
# Short enough to outlast in a test, long enough to survive a slow renewal.
_LEASE_TTL_SECONDS = 2.0
_LEASE_RENEW_SECONDS = 0.2
# The actors below import the TaskStateManager and the lease helpers from
# here. Set per actor: another test module may already have started Ray.
_OPENRAG_RUNTIME_ENV = {"env_vars": {"PYTHONPATH": str(Path(task_state_module.__file__).resolve().parents[2])}}


@pytest.fixture(scope="module")
def local_ray() -> Iterator[None]:
    started = not ray.is_initialized()
    if started:
        # No working_dir: Ray would otherwise package the whole repository.
        ray.init(num_cpus=4, include_dashboard=False, runtime_env={"working_dir": None}, log_to_driver=False)
    # The actors below live in this test module, which Ray's workers cannot import.
    ray.cloudpickle.register_pickle_by_value(sys.modules[__name__])
    try:
        yield
    finally:
        ray.cloudpickle.unregister_pickle_by_value(sys.modules[__name__])
        if started:
            ray.shutdown()


@ray.remote
class _Worker:
    async def run(self, seconds: float) -> dict[str, bool]:
        await asyncio.sleep(seconds)
        return {"stored": True}

    async def fail(self) -> None:
        raise ValueError("unreadable file")


@ray.remote
class _Owner:
    """Plays the pool actor: it submits the worker task, so it owns the ref."""

    def __init__(self, worker: ray.actor.ActorHandle) -> None:
        self._worker = worker

    def submit(self, seconds: float) -> list[ray.ObjectRef]:
        return [self._worker.run.remote(seconds)]


def _status_once_ready(ref: ray.ObjectRef) -> str:
    ray.wait([ref], num_returns=1, timeout=_SETTLE_TIMEOUT_SECONDS)
    return _object_ref_status({"ref": ref})


def _wait_for_status(ref: ray.ObjectRef, expected: str) -> str:
    deadline = time.monotonic() + _SETTLE_TIMEOUT_SECONDS
    status = _object_ref_status({"ref": ref})
    while status != expected and time.monotonic() < deadline:
        time.sleep(0.1)
        status = _object_ref_status({"ref": ref})
    return status


def test_a_running_worker_is_pending_until_it_returns(local_ray) -> None:
    worker = _Worker.remote()
    ref = worker.run.remote(1.0)

    assert _object_ref_status({"ref": ref}) == _REF_PENDING
    assert _status_once_ready(ref) == _REF_SETTLED


def test_a_worker_that_raised_or_died_is_settled(local_ray) -> None:
    worker = _Worker.remote()
    assert _status_once_ready(worker.fail.remote()) == _REF_SETTLED

    doomed = _Worker.remote()
    running = doomed.run.remote(60.0)
    ray.kill(doomed, no_restart=True)
    assert _status_once_ready(running) == _REF_SETTLED


def test_a_ref_whose_owner_died_is_orphaned_while_the_worker_still_runs(local_ray) -> None:
    worker = _Worker.options(max_concurrency=2).remote()
    owner = _Owner.remote(worker)
    [ref] = ray.get(owner.submit.remote(60.0))
    assert _object_ref_status({"ref": ref}) == _REF_PENDING

    ray.kill(owner, no_restart=True)

    assert _wait_for_status(ref, _REF_OWNER_DIED) == _REF_OWNER_DIED
    ready, _ = ray.wait([ref], timeout=0)
    assert ready, "Ray reports an orphaned ref ready, which is why readiness alone is not settlement"
    # The worker is still alive and serving: the orphaned ref said nothing about it.
    assert ray.get(worker.run.remote(0.0), timeout=_SETTLE_TIMEOUT_SECONDS) == {"stored": True}
    ray.kill(worker, no_restart=True)


def test_the_missing_ref_is_pending(local_ray) -> None:
    assert _object_ref_status(None) == _REF_PENDING
    assert _object_ref_status({"ref": None}) == _REF_PENDING


class _QuietLogger:
    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass


@ray.remote
class _Gate:
    """Holds the first task on the worker so the next one has to queue."""

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._waiting = 0

    async def wait(self) -> None:
        self._waiting += 1
        await self._open.wait()

    async def waiting(self) -> int:
        return self._waiting

    async def open(self) -> None:
        self._open.set()


@ray.remote
class _Writes:
    """What the workers wrote, in order: a task that runs twice shows up twice."""

    def __init__(self) -> None:
        self._writes: list[str] = []

    async def record(self, task_id: str) -> None:
        self._writes.append(task_id)

    async def all(self) -> list[str]:
        return list(self._writes)


@ray.remote
class _LeasedWorker:
    """``IndexerWorkerActor.process_file`` reduced to its lease protocol."""

    def __init__(self, task_state_manager: Any, gate: Any, writes: Any) -> None:
        self._task_state_manager = task_state_manager
        self._gate = gate
        self._writes = writes

    async def process_file(self, task_id: str, hold: bool) -> str:
        from services.workers.worker_lease import end_worker_lease, keep_worker_lease

        working = True
        lease_started = asyncio.Event()
        lease = asyncio.create_task(
            keep_worker_lease(
                self._task_state_manager,
                task_id,
                worker_task=asyncio.current_task(),
                logger=_QuietLogger(),
                renew_interval=_LEASE_RENEW_SECONDS,
                started=lease_started,
                is_working=lambda: working,
            )
        )
        try:
            await lease_started.wait()
            # IndexerWorker's first step: a cancelled task is refused here.
            if not await self._task_state_manager.set_state.remote(task_id, "SERIALIZING"):
                raise RuntimeError(f"Task {task_id} was cancelled before indexing started")
            if hold:
                await self._gate.wait.remote()
            await self._writes.record.remote(task_id)
            return task_id
        finally:
            working = False
            lease.cancel()
            await asyncio.gather(lease, return_exceptions=True)
            await end_worker_lease(self._task_state_manager, task_id, logger=_QuietLogger())


@ray.remote
class _Pool:
    """Plays ``IndexerPool``: it submits the task and registers the ref, so it owns it."""

    def __init__(self, task_state_manager: Any, worker: Any) -> None:
        self._task_state_manager = task_state_manager
        self._worker = worker

    async def submit(self, task_id: str, hold: bool) -> list[ray.ObjectRef]:
        from services.workers.worker_lease import worker_registration

        registration = worker_registration(self._worker)
        ref = self._worker.process_file.remote(task_id, hold)
        assert await self._task_state_manager.set_object_ref.remote(task_id, {"ref": ref, **registration}) is True
        return [ref]


def _poll(condition: Any, *, timeout: float = _SETTLE_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


class _Cluster:
    """One worker running ``held`` and holding ``queued`` in its queue, both owned by a pool."""

    def __init__(self, *, worker_max_restarts: int = 0) -> None:
        self.tsm = TaskStateManager.options(runtime_env=_OPENRAG_RUNTIME_ENV).remote(
            worker_lease_ttl_seconds=_LEASE_TTL_SECONDS
        )
        self.gate = _Gate.remote()
        self.writes = _Writes.remote()
        self.worker = _LeasedWorker.options(
            max_concurrency=1, max_restarts=worker_max_restarts, runtime_env=_OPENRAG_RUNTIME_ENV
        ).remote(self.tsm, self.gate, self.writes)
        self.pool = _Pool.options(runtime_env=_OPENRAG_RUNTIME_ENV).remote(self.tsm, self.worker)
        suffix = uuid.uuid4().hex[:8]
        self.held, self.queued = f"held-{suffix}", f"queued-{suffix}"
        self.refs: dict[str, ray.ObjectRef] = {}
        for task_id, hold in ((self.held, True), (self.queued, False)):
            ray.get(
                self.tsm.set_queued_details.remote(
                    task_id, file_id=f"file-{task_id}", partition="tenant-a", metadata={}, user_id=42
                )
            )
            [self.refs[task_id]] = ray.get(self.pool.submit.remote(task_id, hold))
            if hold:
                assert _poll(lambda: ray.get(self.gate.waiting.remote()) == 1)

    def kill_pool(self) -> None:
        ray.kill(self.pool, no_restart=True)
        for ref in self.refs.values():
            assert _wait_for_status(ref, _REF_OWNER_DIED) == _REF_OWNER_DIED

    def settled(self, task_id: str) -> bool:
        return ray.get(self.tsm.has_worker_settled.remote(task_id))

    def admits_a_second_upload_of(self, task_id: str) -> bool:
        outcome = ray.get(
            self.tsm.set_queued_details_v2.remote(
                f"retry-{uuid.uuid4().hex[:8]}",
                file_id=f"file-{task_id}",
                partition="tenant-a",
                metadata={},
                user_id=42,
                reject_if_file_active=True,
            )
        )
        if outcome["accepted"]:
            return True
        assert outcome == {"accepted": False, "reason": "file_indexing", "existing_task_id": task_id}
        return False

    def written(self) -> list[str]:
        return ray.get(self.writes.all.remote())


def test_a_task_queued_behind_a_busy_worker_keeps_its_file_after_the_pool_died(local_ray) -> None:
    """Ray still runs the queued task once the worker frees up; releasing its file earlier lets a retry race it."""
    cluster = _Cluster()
    cluster.kill_pool()

    time.sleep(2 * _LEASE_TTL_SECONDS)

    assert cluster.settled(cluster.queued) is False, "a queued task has no lease, so it cannot have lapsed"
    assert cluster.admits_a_second_upload_of(cluster.queued) is False
    assert cluster.settled(cluster.held) is False, "the running worker still renews its lease"
    assert cluster.written() == []

    ray.get(cluster.gate.open.remote())

    assert _poll(lambda: cluster.settled(cluster.queued))
    assert cluster.written() == [cluster.held, cluster.queued], "the orphaned queued task did run"
    assert _poll(lambda: cluster.settled(cluster.held))
    assert cluster.admits_a_second_upload_of(cluster.queued) is True
    ray.kill(cluster.worker, no_restart=True)


def test_a_queued_task_settles_once_its_worker_actor_died(local_ray) -> None:
    cluster = _Cluster()
    cluster.kill_pool()
    assert cluster.settled(cluster.queued) is False

    ray.kill(cluster.worker, no_restart=True)

    assert _poll(lambda: cluster.settled(cluster.queued), timeout=_LEASE_TTL_SECONDS)
    assert cluster.admits_a_second_upload_of(cluster.queued) is True
    # The running task had started, so its lease lapses on its own.
    assert _poll(lambda: cluster.settled(cluster.held))
    assert cluster.written() == []


def test_a_queued_task_settles_once_its_worker_actor_restarted(local_ray) -> None:
    """A restart drops the queue, and no owner is left to resubmit the task."""
    cluster = _Cluster(worker_max_restarts=1)
    cluster.kill_pool()
    assert cluster.settled(cluster.queued) is False

    ray.kill(cluster.worker, no_restart=False)

    assert _poll(lambda: cluster.settled(cluster.queued))
    time.sleep(2 * _LEASE_TTL_SECONDS)
    assert cluster.written() == []
    ray.kill(cluster.worker, no_restart=True)


def test_cancelling_orphaned_tasks_stops_the_running_one_and_the_queued_one(local_ray) -> None:
    """What a delete relies on: ray.cancel cannot reach these workers, so they must stop themselves."""
    cluster = _Cluster()
    cluster.kill_pool()

    for task_id in (cluster.held, cluster.queued):
        assert ray.get(cluster.tsm.set_cancelled_if_active.remote(task_id)) is True

    # The running worker reads the cancellation on its next renewal and returns
    # well before its lease could lapse; the queued one is refused at pickup.
    assert _poll(lambda: cluster.settled(cluster.held), timeout=_LEASE_TTL_SECONDS / 2)
    assert _poll(lambda: cluster.settled(cluster.queued))
    assert ray.get(cluster.gate.waiting.remote()) == 1
    assert cluster.written() == []
    ray.kill(cluster.worker, no_restart=True)
