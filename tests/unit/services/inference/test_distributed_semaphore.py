import asyncio
import contextlib
import uuid

import pytest
import ray

# Prevent Ray from scanning the working directory (which may contain
# permission-restricted folders like db/) and from packaging the whole repo.
if not ray.is_initialized():
    ray.init(runtime_env={"working_dir": None}, ignore_reinit_error=True)

from services.inference.distributed_semaphore import DistributedSemaphore, DistributedSemaphoreActor  # noqa: E402


class TestDistributedSemaphore:
    def test_default_params(self):
        sem = DistributedSemaphore()
        assert sem._name == "llmSemaphore"
        assert sem._namespace == "openrag"
        assert sem._max_concurrent_ops == 10

    def test_custom_params(self):
        sem = DistributedSemaphore(name="vlm", namespace="test", max_concurrent_ops=5)
        assert sem._name == "vlm"
        assert sem._namespace == "test"
        assert sem._max_concurrent_ops == 5

    def test_actor_class_exists(self):
        assert hasattr(DistributedSemaphoreActor, "remote")


class TestDistributedSemaphoreCancellationSafety:
    """Regression tests for issue #630: cancelling a caller mid-``acquire``
    must not leak the permit forever, since ``acquire.remote()`` keeps
    running on the actor after the local await is cancelled.
    """

    def _new_pool(self, max_concurrent_ops: int = 1) -> DistributedSemaphore:
        # Unique namespace per test so actors from different tests never collide.
        namespace = f"test-sem-{uuid.uuid4().hex}"
        return DistributedSemaphore(name="sem", namespace=namespace, max_concurrent_ops=max_concurrent_ops)

    async def test_cancelling_a_blocked_acquire_does_not_leak_the_permit(self):
        holder = self._new_pool()
        waiter = DistributedSemaphore(name=holder._name, namespace=holder._namespace, max_concurrent_ops=1)

        await holder.__aenter__()  # take the only permit

        blocked_acquire = asyncio.ensure_future(waiter.__aenter__())
        await asyncio.sleep(0.2)  # let the acquire actually reach the actor and start waiting
        blocked_acquire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked_acquire

        await holder.__aexit__(None, None, None)  # release the original permit
        await asyncio.sleep(0.5)  # give the shielded acquire + its release callback time to run

        # If the permit leaked, this would hang until the wait_for timeout fires.
        fresh = DistributedSemaphore(name=holder._name, namespace=holder._namespace, max_concurrent_ops=1)
        await asyncio.wait_for(fresh.__aenter__(), timeout=2.0)
        await fresh.__aexit__(None, None, None)

    async def test_uncancelled_acquire_still_works(self):
        sem = self._new_pool()
        await sem.__aenter__()
        await sem.__aexit__(None, None, None)


class TestDistributedSemaphoreActorRestartSafety:
    """A release deferred past an actor restart (``max_restarts=5``) must not
    over-count the freshly-restarted actor's semaphore above
    ``max_concurrent_ops`` (codex review comment on PR #719).
    """

    async def test_stale_release_after_restart_is_dropped(self):
        namespace = f"test-restart-{uuid.uuid4().hex}"
        actor = DistributedSemaphoreActor.options(
            name="sem",
            namespace=namespace,
            lifetime="detached",
        ).remote(1)

        # Take the only permit and remember the incarnation it was granted under.
        stale_incarnation = await actor.acquire.remote()

        ray.kill(actor, no_restart=False)
        await asyncio.sleep(1.0)  # let Ray restart the actor (fresh __init__, fresh incarnation)

        # A release carrying the pre-restart incarnation must be dropped, not
        # applied to the freshly-restarted (and already full-capacity) actor.
        await actor.release.remote(stale_incarnation)

        # The restarted actor starts with exactly 1 free permit. If the stale
        # release above had incorrectly landed, capacity would be 2 and both
        # of these acquires would succeed immediately.
        await asyncio.wait_for(actor.acquire.remote(), timeout=2.0)
        second = asyncio.ensure_future(actor.acquire.remote())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(second), timeout=0.5)
        finally:
            second.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await second
