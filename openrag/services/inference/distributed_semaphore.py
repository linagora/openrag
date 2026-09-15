"""Ray-based distributed semaphore for cluster-wide concurrency limiting.

Extracted from ``components/utils.py``.  The actor handles acquire/release;
``DistributedSemaphore`` locates (or creates) the actor and wraps it as an
async context manager.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
import weakref

import ray
from core.utils.logging import get_logger

logger = get_logger()


@ray.remote(max_restarts=5)
class DistributedSemaphoreActor:
    def __init__(self, max_concurrent_ops: int):
        self.semaphore = asyncio.Semaphore(max_concurrent_ops)
        # Regenerated every time the actor (re)starts, so a release that was
        # queued against a prior incarnation - e.g. one deferred past a
        # cancellation - can be detected and dropped instead of incrementing
        # a freshly-restarted semaphore above max_concurrent_ops.
        self.incarnation = uuid.uuid4().hex

    async def acquire(self) -> str:
        await self.semaphore.acquire()
        return self.incarnation

    def release(self, incarnation: str | None = None) -> None:
        # `incarnation` defaults to None so a driver still running the
        # pre-incarnation-token code (rolling deploy against this same
        # detached, `get_if_exists=True` actor) can call `release()` with no
        # arguments without raising - it never checked incarnations either.
        if incarnation is not None and incarnation != self.incarnation:
            return
        self.semaphore.release()


# One admission gate per (event loop, semaphore name, budget). The remote
# semaphore still enforces the cluster-wide budget; this bounds only how many
# ``acquire`` *calls* this process has in flight on the actor at any moment.
# Holders are deliberately not gated - see __aenter__.
#
# Why it exists: ``DistributedSemaphoreActor`` is an asyncio Ray actor, so it
# runs at Ray's default ``max_concurrency`` of 1000, and a waiting ``acquire``
# occupies one of those slots for its whole wait. Queued actor calls are
# dispatched in arrival order, so once blocked acquires fill every slot a later
# ``release`` is never dispatched: no permit is returned, no waiter wakes, and
# the semaphore stays wedged for the lifetime of the (detached) actor. Capping
# outstanding acquires per process at the permit count stops that pile-up from
# forming - a process can never usefully hold more permits than exist, so
# admitting more than ``budget`` locally buys nothing (#965).
#
# Keyed by the running loop because an ``asyncio.Semaphore`` parks its waiters on
# the loop that created them and must not be shared across loops: Ray runs each
# concurrency group on its own loop, and the API and worker processes have their
# own. Weak keys so a finished loop's gates are collected along with it.
_local_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, int], asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)


def _local_gate(name: str, budget: int) -> asyncio.Semaphore:
    """Return this loop's admission gate for ``name``, creating it on first use."""
    loop = asyncio.get_running_loop()
    per_loop = _local_gates.get(loop)
    if per_loop is None:
        per_loop = {}
        _local_gates[loop] = per_loop
    key = (name, budget)
    gate = per_loop.get(key)
    if gate is None:
        # max(1, ...) so a misconfigured non-positive budget degrades to serial
        # access rather than an asyncio.Semaphore that never admits anyone.
        gate = asyncio.Semaphore(max(1, budget))
        per_loop[key] = gate
    return gate


class DistributedSemaphore:
    """Async context manager backed by a detached Ray actor.

    The actor is created on first use (get-or-create) and survives across
    callers within the same Ray cluster. Instances are routinely shared and
    entered concurrently by many callers at once (e.g. one cluster-wide
    ``llmSemaphore`` used by every call in a batch of chunk-contextualization
    tasks), so the incarnation token from each acquire is tracked per calling
    task rather than on ``self``: storing it on ``self`` would let one
    caller's concurrent ``__aenter__`` overwrite another's before its
    ``__aexit__`` reads it back, misattributing a release to the wrong
    incarnation after an actor restart.

    Entering also takes a small **process-local** admission gate for the
    duration of the acquire call (see :func:`_local_gate`), so a burst of callers
    queues inside this process instead of as thousands of outstanding calls on
    the actor.

    ``acquire_timeout`` bounds how long a caller waits for a permit. Waiting for
    a permit is otherwise the only unbounded wait on the enrichment path - the
    inference calls themselves are already bounded by their client's httpx
    timeout - so without it a saturated gate pins an indexer slot indefinitely.
    ``None`` waits forever, preserving the previous behaviour for callers that
    have not been given a bound.
    """

    def __init__(
        self,
        name: str = "llmSemaphore",
        namespace: str = "openrag",
        max_concurrent_ops: int = 10,
        acquire_timeout: float | None = None,
    ):
        self._name = name
        self._namespace = namespace
        self._max_concurrent_ops = max_concurrent_ops
        self._acquire_timeout = acquire_timeout
        self._incarnations: dict[asyncio.Task, list[str | None]] = {}

    def _get_or_create_actor(self):
        try:
            return ray.get_actor(self._name, namespace=self._namespace)
        except ValueError:
            return DistributedSemaphoreActor.options(
                name=self._name,
                namespace=self._namespace,
                lifetime="detached",
            ).remote(self._max_concurrent_ops)

    async def __aenter__(self):
        # Local admission gate before the actor is touched at all, so a burst
        # cannot fill the actor's concurrency slots with waiters (#965).
        gate = _local_gate(self._name, self._max_concurrent_ops)
        await gate.acquire()
        try:
            semaphore_actor = self._get_or_create_actor()
            # acquire.remote() is dispatched to the actor immediately and runs to
            # completion there regardless of what happens locally - cancelling the
            # local await does not cancel the remote task. Shield the wait so a
            # cancellation here doesn't just abandon a permit that the actor may
            # still grant a moment later: if that happens, __aenter__ never
            # returns, __aexit__ never runs, and the permit would otherwise leak
            # for the lifetime of the actor.
            acquire_task = asyncio.ensure_future(semaphore_actor.acquire.remote())
            try:
                incarnation = await _await_permit(acquire_task, self._acquire_timeout)
            except TimeoutError:
                # Same hand-back as the cancellation path below: the actor may
                # still grant this permit, and nothing local can stop it.
                acquire_task.add_done_callback(functools.partial(_release_if_granted, self._name, semaphore_actor))
                logger.bind(semaphore=self._name, acquire_timeout=self._acquire_timeout).warning(
                    "Timed out waiting for a permit on semaphore '{name}' - giving up rather than "
                    "holding an indexing slot on a saturated gate.",
                    name=self._name,
                )
                raise TimeoutError(
                    f"Timed out after {self._acquire_timeout:g}s waiting for a permit on '{self._name}'"
                ) from None
            except asyncio.CancelledError:
                acquire_task.add_done_callback(functools.partial(_release_if_granted, self._name, semaphore_actor))
                raise
        finally:
            # Only the *call* is gated, never the held section: an acquire that
            # has returned no longer occupies an actor slot, so a holder costs
            # the actor nothing and must not consume admission. Gating the hold
            # instead would also make a local budget that an actor restart
            # cannot reset - the restart is what frees capacity after a holder
            # wedges, and a gated hold would defeat that recovery.
            gate.release()
        self._incarnations.setdefault(asyncio.current_task(), []).append(incarnation)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        semaphore_actor = self._get_or_create_actor()
        task = asyncio.current_task()
        stack = self._incarnations.get(task)
        incarnation = stack.pop() if stack else None
        if stack is not None and not stack:
            del self._incarnations[task]
        await _release(semaphore_actor, incarnation)


async def _await_permit(acquire_task: asyncio.Task, timeout: float | None):
    """Await a dispatched acquire, optionally bounded.

    Shielded either way: ``acquire.remote()`` runs to completion on the actor
    regardless of what happens locally, so abandoning the local await must not
    abandon a permit the actor may still grant. ``wait_for`` cancels the shield,
    never the task behind it, which leaves the caller free to hand the permit
    back through the usual done-callback.
    """
    if timeout is None:
        return await asyncio.shield(acquire_task)
    return await asyncio.wait_for(asyncio.shield(acquire_task), timeout=timeout)


async def _release(semaphore_actor, incarnation: str | None) -> None:
    """Call ``release`` with the right arity for whichever actor is live.

    ``incarnation`` is ``None`` when talking to a pre-incarnation-token actor
    left running from before this deploy (detached actors are looked up with
    ``get_if_exists=True``, so a rolling deploy can attach to one instead of
    recreating it) - its ``acquire()`` never returned a token, and its
    ``release()`` takes no arguments, so passing one would raise ``TypeError``
    and leak the permit.
    """
    if incarnation is None:
        await semaphore_actor.release.remote()
    else:
        await semaphore_actor.release.remote(incarnation)


def _release_if_granted(name: str, semaphore_actor, acquire_task: asyncio.Task) -> None:
    """Release a permit granted to an acquire whose caller was already cancelled.

    Runs as a done-callback on the (shielded, still-running) acquire task, so
    it fires once the actor eventually grants or drops the request. Passes
    along the incarnation token from that same grant so a release delayed
    past an actor restart is dropped by the actor instead of over-counting
    the freshly-restarted semaphore.
    """
    if acquire_task.cancelled() or acquire_task.exception() is not None:
        return
    incarnation = acquire_task.result()
    logger.bind(semaphore=name).warning(
        "Releasing permit for a cancelled acquire on semaphore '{name}' - "
        "caller was cancelled while waiting, permit granted afterwards.",
        name=name,
    )
    asyncio.ensure_future(_release(semaphore_actor, incarnation))
