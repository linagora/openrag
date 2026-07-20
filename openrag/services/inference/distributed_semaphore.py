"""Ray-based distributed semaphore for cluster-wide concurrency limiting.

Extracted from ``components/utils.py``.  The actor handles acquire/release;
``DistributedSemaphore`` locates (or creates) the actor and wraps it as an
async context manager.
"""

from __future__ import annotations

import asyncio
import functools

import ray
from core.utils.logging import get_logger

logger = get_logger()


@ray.remote(max_restarts=5)
class DistributedSemaphoreActor:
    def __init__(self, max_concurrent_ops: int):
        self.semaphore = asyncio.Semaphore(max_concurrent_ops)

    async def acquire(self):
        await self.semaphore.acquire()

    def release(self):
        self.semaphore.release()


class DistributedSemaphore:
    """Async context manager backed by a detached Ray actor.

    The actor is created on first use (get-or-create) and survives across
    callers within the same Ray cluster.
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
            await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            acquire_task.add_done_callback(functools.partial(_release_if_granted, self._name, semaphore_actor))
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        semaphore_actor = self._get_or_create_actor()
        await semaphore_actor.release.remote()


def _release_if_granted(name: str, semaphore_actor, acquire_task: asyncio.Task) -> None:
    """Release a permit granted to an acquire whose caller was already cancelled.

    Runs as a done-callback on the (shielded, still-running) acquire task, so
    it fires once the actor eventually grants or drops the request.
    """
    if acquire_task.cancelled() or acquire_task.exception() is not None:
        return
    logger.bind(semaphore=name).warning(
        "Releasing permit for a cancelled acquire on semaphore '{name}' - "
        "caller was cancelled while waiting, permit granted afterwards.",
        name=name,
    )
    semaphore_actor.release.remote()
