"""``_object_ref_status`` against real Ray refs, including one whose owner died.

The TaskStateManager tests drive the fence through a fake ref; these pin the
Ray behaviour that fake mirrors. The worker ref is owned by the pool actor that
submitted the task, so killing that actor while the worker runs is exactly the
case the review found: ``ray.wait`` reports the ref ready, and only
``ray.get`` raising ``OwnerDiedError`` tells it apart from a settled worker.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator

import pytest
import ray
import ray.cloudpickle
from services.workers.task_state import _REF_OWNER_DIED, _REF_PENDING, _REF_SETTLED, _object_ref_status

_SETTLE_TIMEOUT_SECONDS = 30.0


@pytest.fixture(scope="module")
def local_ray() -> Iterator[None]:
    started = not ray.is_initialized()
    if started:
        # No working_dir: Ray would otherwise package the whole repository.
        ray.init(num_cpus=2, include_dashboard=False, runtime_env={"working_dir": None}, log_to_driver=False)
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
