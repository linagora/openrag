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
    WORKER_ACTOR_ID_KEY,
    WORKER_LEASE_RENEW_INTERVAL_SECONDS,
    WORKER_RESTARTS_KEY,
    read_worker_actor_state,
)


def worker_ref_registration(ref: Any, worker: Any) -> dict[str, Any]:
    """The ``set_object_ref`` payload for a task just sent to ``worker``.

    Records the worker actor and its current restart count, read from the
    GCS. The count can be late both ways. A restart that lands between the
    submission and this read drops the task yet counts as its incarnation, so
    the task holds its file until the next restart. Worse, an actor that
    crashed before the GCS noticed still reads ALIVE with the old count while
    the task goes to the incarnation after it: that restart then reads as one
    past the task, which settles it while it may still run. The task state
    therefore marks a task it settles this way abandoned, and refuses its
    pickup (``TaskStateManager.renew_worker_lease``) so it is dropped unindexed.
    """
    registration: dict[str, Any] = {"ref": ref}
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
    started: asyncio.Event | None = None,
    is_working: Callable[[], bool] | None = None,
) -> None:
    """Renew the task's lease until cancelled, starting the moment work begins.

    The first renewal tells the TaskStateManager the task left the worker
    actor's queue, and ``started`` is set once it was answered (or failed), so
    the worker can wait for it before doing any work. A renewal answered
    ``False`` means the task was cancelled, or abandoned while queued:
    ``worker_task`` is cancelled then, since ``ray.cancel`` on a ref whose owner
    died does not reach the worker, and this is the one thing that still can.
    It is left alone once ``is_working`` says the work is over, so a late
    answer cannot cut short the cleanup that follows it.
    """
    remote = _remote_method(task_state_manager, "renew_worker_lease")
    if remote is None:
        if started is not None:
            started.set()
        return
    interval = WORKER_LEASE_RENEW_INTERVAL_SECONDS if renew_interval is None else renew_interval
    while True:
        renewed = None
        try:
            # Awaited directly: the shared timeout helper ray.cancel()s the
            # call when this loop is cancelled, and a renewal needs no undo.
            (renewed,) = await asyncio.wait_for(asyncio.gather(remote(task_id)), timeout=interval)
        except Exception as exc:  # noqa: BLE001 - a missed renewal must never fail the file
            logger.warning(f"Failed to renew the worker lease of task {task_id}: {exc}")
        if renewed is False and (is_working is None or is_working()):
            logger.warning(f"Task {task_id} was cancelled or abandoned; stopping its worker")
            worker_task.cancel()
        if started is not None:
            started.set()
        if renewed is False:
            return
        await asyncio.sleep(interval)


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
