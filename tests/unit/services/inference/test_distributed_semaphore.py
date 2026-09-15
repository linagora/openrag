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


class TestDistributedSemaphoreSharedInstanceConcurrencySafety:
    """A single ``DistributedSemaphore`` instance is routinely entered by many
    concurrent callers at once (e.g. the cluster-wide ``llmSemaphore`` shared
    across a batch of chunk-contextualization calls). The incarnation token
    must be tracked per calling task, not on ``self`` - otherwise a
    concurrent caller's acquire (after an actor restart) can overwrite
    another's release token, causing a stale release to be misattributed to
    the new incarnation and over-releasing the freshly-restarted semaphore
    (self-review comment on PR #719).
    """

    async def test_concurrent_holder_release_is_not_clobbered_by_a_sibling_acquire_after_restart(self):
        namespace = f"test-shared-{uuid.uuid4().hex}"
        sem = DistributedSemaphore(name="sem", namespace=namespace, max_concurrent_ops=1)

        entered = asyncio.Event()
        release_now = asyncio.Event()

        async def hold_across_restart():
            async with sem:
                entered.set()
                await release_now.wait()

        # Task A takes the only permit under the pre-restart incarnation and
        # blocks inside the critical section.
        task_a = asyncio.ensure_future(hold_across_restart())
        await asyncio.wait_for(entered.wait(), timeout=2.0)

        actor = sem._get_or_create_actor()
        ray.kill(actor, no_restart=False)
        await asyncio.sleep(1.0)  # let Ray restart the actor: fresh semaphore, fresh incarnation

        # Task B reuses the SAME shared `sem` instance and acquires after the
        # restart - capacity is free again post-restart, so this succeeds
        # immediately. Pre-fix, this would overwrite `sem._incarnation`.
        await asyncio.wait_for(sem.__aenter__(), timeout=2.0)

        # Let task A's (pre-restart) hold release. If its incarnation token
        # was clobbered by task B's acquire, the actor would wrongly accept
        # the release and the freshly-restarted semaphore would end up
        # over-capacity.
        release_now.set()
        await asyncio.wait_for(task_a, timeout=2.0)

        # Capacity is 1 and task B is still holding it: a third acquire must
        # block, proving task A's release did not land on task B's incarnation.
        third = asyncio.ensure_future(sem.__aenter__())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(third), timeout=0.5)
        finally:
            third.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await third


@ray.remote(max_restarts=5)
class _LegacyDistributedSemaphoreActor:
    """Mirrors the pre-incarnation-token ``DistributedSemaphoreActor``:
    ``acquire()`` returns nothing and ``release()`` takes zero arguments.

    Detached actors are looked up with ``get_if_exists=True`` at bootstrap, so
    a rolling deploy attaches to whatever actor is already running under that
    name instead of recreating it - this simulates one left over from before
    the incarnation-token fix.
    """

    def __init__(self, max_concurrent_ops: int):
        self.semaphore = asyncio.Semaphore(max_concurrent_ops)

    async def acquire(self):
        await self.semaphore.acquire()

    def release(self):
        self.semaphore.release()


class TestDistributedSemaphoreRollingDeployCompatibility:
    """New driver code must not crash against a pre-existing legacy actor
    during a rolling deploy (hedhoud + CodeRabbit review comments on PR #719).
    """

    async def test_new_driver_survives_a_pre_existing_legacy_actor(self):
        namespace = f"test-legacy-{uuid.uuid4().hex}"
        _LegacyDistributedSemaphoreActor.options(
            name="sem",
            namespace=namespace,
            lifetime="detached",
        ).remote(1)

        sem = DistributedSemaphore(name="sem", namespace=namespace, max_concurrent_ops=1)

        # Must not raise TypeError: the legacy actor's release() takes no
        # arguments, so a bare `release.remote(incarnation)` would fail.
        async with sem:
            pass

        # The permit must actually have been released - a second acquire
        # right after must succeed rather than hang.
        await asyncio.wait_for(sem.__aenter__(), timeout=2.0)
        await sem.__aexit__(None, None, None)


class _StubActorMethod:
    """Stands in for a Ray actor method handle (``handle.method.remote(...)``)."""

    def __init__(self, fn):
        self._fn = fn

    def remote(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _StubSemaphoreActor:
    """In-process stand-in for ``DistributedSemaphoreActor``.

    Records how many ``acquire`` calls are outstanding at once — the quantity
    that occupies Ray actor concurrency slots and, past the actor's limit,
    starves ``release`` (#965). A stub lets that invariant be asserted directly
    instead of needing a thousand concurrent callers to observe it.
    """

    def __init__(self, permits: int):
        self._permits = asyncio.Semaphore(permits)
        self.incarnation = "stub-incarnation"
        self.acquire_calls = 0
        self.release_calls = 0
        self.outstanding_acquires = 0
        self.max_outstanding_acquires = 0
        self.acquire = _StubActorMethod(self._acquire)
        self.release = _StubActorMethod(self._release)

    def grant(self, count: int = 1) -> None:
        for _ in range(count):
            self._permits.release()

    async def _acquire(self) -> str:
        self.acquire_calls += 1
        self.outstanding_acquires += 1
        self.max_outstanding_acquires = max(self.max_outstanding_acquires, self.outstanding_acquires)
        try:
            await self._permits.acquire()
        finally:
            self.outstanding_acquires -= 1
        return self.incarnation

    async def _release(self, incarnation: str | None = None) -> None:
        self.release_calls += 1
        self._permits.release()


async def _free_permits(gate: asyncio.Semaphore) -> int:
    """Count a gate's free permits without disturbing it.

    Public-API only, so a double release shows up as a count *above* the
    configured budget rather than being silently absorbed.
    """
    taken = 0
    while True:
        try:
            await asyncio.wait_for(gate.acquire(), timeout=0.01)
        except TimeoutError:
            break
        taken += 1
    for _ in range(taken):
        gate.release()
    return taken


class TestDistributedSemaphoreLocalAdmissionGate:
    """Regression tests for issue #965: a burst of callers must not park more
    outstanding ``acquire`` calls on the actor than the local budget allows.

    ``DistributedSemaphoreActor`` is an asyncio Ray actor running at Ray's
    default ``max_concurrency`` of 1000, and a blocked ``acquire`` holds one of
    those slots for its whole wait. Once waiters fill every slot, a ``release``
    submitted afterwards is never dispatched, no permit is returned, and the
    semaphore stays wedged for the lifetime of the detached actor.

    Only the acquire *call* is gated. A holder's acquire has already returned
    and occupies no actor slot, so holders are not admission-controlled - and
    gating them would create a local budget that an actor restart cannot reset.
    """

    @staticmethod
    def _sem(budget: int, stub: _StubSemaphoreActor) -> DistributedSemaphore:
        # Unique name per test so each gets its own gate even when pytest-asyncio
        # reuses an event loop (gates are keyed by loop *and* name).
        sem = DistributedSemaphore(
            name=f"gate-{uuid.uuid4().hex}",
            namespace="test",
            max_concurrent_ops=budget,
        )
        sem._get_or_create_actor = lambda: stub
        return sem

    def _gate_of(self, sem: DistributedSemaphore) -> asyncio.Semaphore:
        from services.inference.distributed_semaphore import _local_gate

        return _local_gate(sem._name, sem._max_concurrent_ops)

    async def test_outstanding_acquires_never_exceed_the_budget(self):
        budget = 4
        # Fewer permits than the budget so acquires genuinely block on the actor.
        stub = _StubSemaphoreActor(permits=2)
        sem = self._sem(budget, stub)

        async def use():
            async with sem:
                await asyncio.sleep(0)

        await asyncio.gather(*(use() for _ in range(40)))

        assert stub.max_outstanding_acquires <= budget
        assert stub.acquire_calls == 40
        assert stub.release_calls == 40

    async def test_waiting_on_the_gate_does_not_reach_the_actor(self):
        stub = _StubSemaphoreActor(permits=0)  # the first acquire blocks on the actor
        sem = self._sem(1, stub)

        first = asyncio.ensure_future(sem.__aenter__())
        await asyncio.sleep(0.05)
        assert stub.acquire_calls == 1

        second = asyncio.ensure_future(sem.__aenter__())
        await asyncio.sleep(0.05)
        assert stub.acquire_calls == 1, "a caller queued on the local gate must not call the actor"

        # Granting a permit lets the first acquire return, which frees the gate
        # and only then lets the second caller's call reach the actor.
        stub.grant(1)
        await asyncio.wait_for(first, timeout=1.0)
        await asyncio.sleep(0.05)
        assert stub.acquire_calls == 2

        second.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await second

    async def test_cancelled_during_remote_acquire_returns_the_gate_permit_exactly_once(self):
        stub = _StubSemaphoreActor(permits=0)  # nothing will be granted
        sem = self._sem(1, stub)

        blocked = asyncio.ensure_future(sem.__aenter__())
        await asyncio.sleep(0.05)
        assert stub.acquire_calls == 1, "this caller did reach the actor before being cancelled"

        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked

        # Exactly one: a missed release would starve the budget, a double
        # release would widen it permanently.
        assert await _free_permits(self._gate_of(sem)) == 1

    async def test_nested_use_on_one_task_does_not_self_deadlock(self):
        stub = _StubSemaphoreActor(permits=2)
        # Budget of 1 while nesting two deep: safe precisely because the gate
        # covers the acquire call, not the held section.
        sem = self._sem(1, stub)

        async with sem:
            async with sem:
                assert stub.acquire_calls == 2

        assert stub.release_calls == 2
        assert await _free_permits(self._gate_of(sem)) == 1

    async def test_gate_is_released_when_the_remote_acquire_raises(self):
        stub = _StubSemaphoreActor(permits=1)
        sem = self._sem(1, stub)

        async def boom():
            raise RuntimeError("actor unreachable")

        stub.acquire = _StubActorMethod(boom)
        with pytest.raises(RuntimeError):
            await sem.__aenter__()

        # A failed acquire must not shrink this process's admission budget.
        assert await _free_permits(self._gate_of(sem)) == 1

    def test_gates_are_scoped_to_the_running_event_loop(self):
        from services.inference.distributed_semaphore import _local_gate

        seen = []

        async def grab():
            seen.append(_local_gate("loop-scoped-gate", 3))

        asyncio.run(grab())
        asyncio.run(grab())

        # An asyncio.Semaphore parks its waiters on the loop that created them,
        # so a gate must never be carried across loops.
        assert seen[0] is not seen[1]
