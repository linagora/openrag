from __future__ import annotations

import asyncio
import uuid

import pytest
import ray
from core.utils.exceptions import ServiceUnavailableError
from services.orchestrators.job_service import JobService


@ray.remote
class _RestartableTaskState:
    def __init__(self):
        self.states = {}
        self.incarnation = uuid.uuid4().hex

    def set_state(self, task_id: str, state: str) -> None:
        self.states[task_id] = state

    def get_all_states(self) -> dict[str, str]:
        return dict(self.states)

    def get_pool_info(self) -> dict[str, int]:
        return {"total_capacity": 1, "pool_size": 1, "max_tasks_per_worker": 1}

    def get_incarnation(self) -> str:
        return self.incarnation


@pytest.fixture(scope="module", autouse=True)
def _local_ray():
    started_here = not ray.is_initialized()
    if started_here:
        ray.init(runtime_env={"working_dir": None}, include_dashboard=False)
    yield
    if started_here:
        ray.shutdown()


async def _wait_until_available(service: JobService, timeout: float = 10) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await service.get_queue_info()
        except ServiceUnavailableError:
            await asyncio.sleep(0.1)
    raise TimeoutError("JobService did not recover")


async def _wait_for_new_incarnation(actor, previous: str, timeout: float = 10) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await actor.get_incarnation.remote() != previous:
                return
        except ray.exceptions.RayActorError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError("Actor incarnation did not change")


@pytest.mark.integration
@pytest.mark.slow
async def test_cached_job_service_recovers_after_actor_process_restart():
    namespace = f"task-state-recovery-{uuid.uuid4().hex}"
    actor = _RestartableTaskState.options(
        name="TaskStateManager",
        namespace=namespace,
        lifetime="detached",
        max_restarts=-1,
        max_task_retries=0,
    ).remote()

    try:
        await actor.set_state.remote("before-restart", "QUEUED")
        service = JobService(actor, timeout=5)
        assert (await service.get_queue_info())["tasks"]["active"] == 1
        actor_id = actor._actor_id.hex()
        incarnation = await actor.get_incarnation.remote()

        ray.kill(actor, no_restart=False)
        await _wait_for_new_incarnation(actor, incarnation)
        recovered_queue = await _wait_until_available(service)

        assert actor._actor_id.hex() == actor_id
        assert recovered_queue["tasks"]["active"] == 0
        await actor.set_state.remote("after-restart", "QUEUED")
        queue = await service.get_queue_info()
        assert queue["tasks"]["active"] == 1
    finally:
        ray.kill(actor, no_restart=True)
