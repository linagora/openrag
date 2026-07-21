"""Ray-based distributed semaphore for cluster-wide concurrency limiting.

Extracted from ``components/utils.py``.  The actor handles acquire/release;
``DistributedSemaphore`` locates (or creates) the actor and wraps it as an
async context manager.
"""

from __future__ import annotations

import asyncio
import functools
import uuid

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
    """

    def __init__(
        self,
        name: str = "llmSemaphore",
        namespace: str = "openrag",
        max_concurrent_ops: int = 10,
    ):
        self._name = name
        self._namespace = namespace
        self._max_concurrent_ops = max_concurrent_ops
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
            incarnation = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            acquire_task.add_done_callback(functools.partial(_release_if_granted, self._name, semaphore_actor))
            raise
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
