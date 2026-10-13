"""The worker's side of the task lease the TaskStateManager keeps.

The pool actor that submits ``process_file`` owns the worker ref. Once it dies,
Ray reports that ref ready with ``OwnerDiedError`` whatever the worker is doing,
so the worker tells the TaskStateManager itself: that it started, that it is
still running, and that it returned. The pool records which worker actor the
task went to, which settles a task that never started (see
``task_state._worker_ref_has_settled``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from services.workers.task_state import (
    _WORKER_LEASE_TTL_SECONDS,
    STALE_TASK_STATE_MANAGER_HINT,
    WORKER_ACTOR_ID_KEY,
    WORKER_LEASE_RENEW_INTERVAL_SECONDS,
    WORKER_RESTARTS_KEY,
    read_worker_actor_state,
)

# How soon a renewal that failed is tried again, capped by the renewal interval.
_LEASE_RETRY_SECONDS = 2.0


class WorkerLeaseLostError(RuntimeError):
    """The TaskStateManager could not be told the worker runs, so it must not write."""


def worker_registration(worker: Any) -> dict[str, Any]:
    """What ``set_object_ref`` stores next to the ref of a task sent to ``worker``.

    Records the worker actor and its current restart count, read from the
    GCS. The pool reads it BEFORE it submits the task: a restart that lands
    after the read then drops the task from an incarnation past the recorded
    count, which settles it. Read after the submission, that restart would
    count as the task's own incarnation and hold its file until the next one.

    The count can still be early: an actor that crashed before the GCS
    noticed reads ALIVE with the old count while the task goes to the
    incarnation after it, so that restart reads as one past the task and
    settles it while it may still run. The task state therefore marks a task
    it settles this way abandoned, and refuses its pickup
    (``TaskStateManager.renew_worker_lease``) so it is dropped unindexed.
    """
    registration: dict[str, Any] = {}
    actor_id = getattr(worker, "_actor_id", None)
    hex_id = getattr(actor_id, "hex", None)
    actor_hex = hex_id() if callable(hex_id) else None
    if not isinstance(actor_hex, str):
        return registration
    registration[WORKER_ACTOR_ID_KEY] = actor_hex
    state = read_worker_actor_state(actor_hex)
    if state is not None:
        registration[WORKER_RESTARTS_KEY] = state[1]
    return registration


def _remote_method(task_state_manager: Any, name: str) -> Any | None:
    # A TaskStateManager from an earlier release has no lease to renew.
    return getattr(getattr(task_state_manager, name, None), "remote", None)


async def keep_worker_lease(
    task_state_manager: Any,
    task_id: str,
    *,
    worker_task: asyncio.Task[Any],
    logger: Any,
    renew_interval: float | None = None,
    lease_ttl: float | None = None,
    started: asyncio.Event | None = None,
    is_working: Callable[[], bool] | None = None,
) -> None:
    """Renew the task's lease until cancelled, starting the moment work begins.

    The first renewal tells the TaskStateManager the task left the worker
    actor's queue, and ``started`` is set only once it was answered, so the
    worker can wait for it before doing any work. A renewal answered ``False``
    means the task was cancelled, or abandoned while queued: ``worker_task`` is
    cancelled then, since ``ray.cancel`` on a ref whose owner died does not
    reach the worker, and this is the one thing that still can.

    Failing closed: the TaskStateManager settles a started task once its lease
    lapses, so the worker stops one renewal interval before the last answered
    renewal would lapse. A first renewal never answered in that time raises
    ``WorkerLeaseLostError`` with ``started`` unset; a later one cancels
    ``worker_task`` and raises it. One missed renewal is retried and stops
    nothing. ``worker_task`` is left alone once ``is_working`` says the work is
    over, so a late answer cannot cut short the cleanup that follows it.
    """
    remote = _remote_method(task_state_manager, "renew_worker_lease")
    if remote is None:
        logger.warning(f"Task {task_id} runs without a worker lease. {STALE_TASK_STATE_MANAGER_HINT}")
        if started is not None:
            started.set()
        return
    interval = WORKER_LEASE_RENEW_INTERVAL_SECONDS if renew_interval is None else renew_interval
    ttl = _WORKER_LEASE_TTL_SECONDS if lease_ttl is None else lease_ttl
    # Counted from when a renewal was sent, never later than the
    # TaskStateManager started its TTL; the interval left over is for the
    # cancellation to land.
    stop_after = max(ttl - interval, interval)
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + stop_after
    answered = False
    while True:
        sent_at = loop.time()
        if sent_at >= stop_at:
            if not answered:
                raise WorkerLeaseLostError(f"Task {task_id} could not register its worker with the task state")
            if is_working is not None and not is_working():
                return
            logger.warning(f"Task {task_id} could not renew its worker lease; stopping its worker before it lapses")
            worker_task.cancel()
            raise WorkerLeaseLostError(f"Task {task_id} lost its worker lease")
        try:
            # Awaited directly: the shared timeout helper ray.cancel()s the
            # call when this loop is cancelled, and a renewal needs no undo.
            (renewed,) = await asyncio.wait_for(
                asyncio.gather(remote(task_id)), timeout=min(interval, stop_at - sent_at)
            )
        except Exception as exc:  # noqa: BLE001 - one missed renewal must not fail the file
            logger.warning(f"Failed to renew the worker lease of task {task_id}: {exc}")
            await asyncio.sleep(max(0.0, min(_LEASE_RETRY_SECONDS, interval, stop_at - loop.time())))
            continue
        answered = True
        stop_at = sent_at + stop_after
        if renewed is False and (is_working is None or is_working()):
            logger.warning(f"Task {task_id} was cancelled or abandoned; stopping its worker")
            worker_task.cancel()
        if started is not None:
            started.set()
        if renewed is False:
            return
        await asyncio.sleep(interval)


async def wait_for_worker_lease_start(started: asyncio.Event, worker_lease: asyncio.Task[None]) -> None:
    """Return once the lease's first renewal was answered, or raise why it never will be."""
    waiter = asyncio.ensure_future(started.wait())
    try:
        await asyncio.wait({waiter, worker_lease}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if not started.is_set():
        worker_lease.result()
        raise WorkerLeaseLostError("The worker lease ended before its first renewal was answered")


def worker_lease_loss(worker_lease: asyncio.Task[None]) -> WorkerLeaseLostError | None:
    """The error a lease ended with when it stopped the worker, else ``None``."""
    if not worker_lease.done() or worker_lease.cancelled():
        return None
    exc = worker_lease.exception()
    return exc if isinstance(exc, WorkerLeaseLostError) else None


async def end_worker_lease(
    task_state_manager: Any,
    task_id: str,
    *,
    logger: Any,
    timeout: float | None = None,
) -> None:
    """Tell the TaskStateManager this worker returned. Best effort: the lease lapses anyway."""
    remote = _remote_method(task_state_manager, "end_worker_lease")
    if remote is None:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(remote(task_id)),
            timeout=WORKER_LEASE_RENEW_INTERVAL_SECONDS if timeout is None else timeout,
        )
    except Exception as exc:  # noqa: BLE001 - the lease lapses on its own
        logger.warning(f"Failed to end the worker lease of task {task_id}: {exc}")
