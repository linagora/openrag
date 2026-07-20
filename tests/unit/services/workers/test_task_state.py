from __future__ import annotations

from typing import Any

import pytest
from services.workers import task_state as task_state_module
from services.workers.task_state import PENDING_TASK_DETAILS, TaskStateManager


def _task_state_manager() -> Any:
    return TaskStateManager.__ray_metadata__.modified_class()


@pytest.mark.asyncio
async def test_cancelled_state_is_not_overwritten_by_worker_transitions() -> None:
    """A cancel claim is sticky: a worker still in flight cannot report over it (#685)."""
    manager = _task_state_manager()

    await manager.set_state("task-1", "QUEUED")
    assert await manager.set_cancelled_if_active("task-1") is True

    await manager.set_state("task-1", "SERIALIZING")
    await manager.set_state("task-1", "COMPLETED")

    assert await manager.get_state("task-1") == "CANCELLED"


@pytest.mark.asyncio
async def test_set_object_ref_accepts_terminal_states_without_reopening_task() -> None:
    manager = _task_state_manager()

    await manager.set_state("completed-task", "COMPLETED")
    await manager.set_state("failed-task", "FAILED")
    await manager.set_state("cancelled-task", "CANCELLED")

    assert await manager.set_object_ref("completed-task", {"ref": object()}) is True
    assert await manager.set_object_ref("failed-task", {"ref": object()}) is True
    assert await manager.set_object_ref("cancelled-task", {"ref": object()}) is False

    assert await manager.get_state("completed-task") == "COMPLETED"
    assert await manager.get_state("failed-task") == "FAILED"
    assert await manager.get_state("cancelled-task") == "CANCELLED"


@pytest.mark.asyncio
async def test_set_queued_details_records_active_state_and_routing_together() -> None:
    manager = _task_state_manager()

    accepted = await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"filename": "report.txt"},
        user_id=42,
    )

    assert accepted is True
    assert await manager.get_state("task-1") == "QUEUED"
    assert await manager.get_details("task-1") == {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"filename": "report.txt"},
        "user_id": 42,
    }
    assert "task-1" in await manager.get_all_user_info(42)


@pytest.mark.asyncio
async def test_matching_active_task_refs_treat_detail_less_queued_tasks_as_pending_registration() -> None:
    manager = _task_state_manager()
    ref = object()

    await manager.set_state("queued-without-details", "QUEUED")
    await manager.set_object_ref("queued-without-details", {"ref": ref})
    await manager.set_state("completed-without-details", "COMPLETED")
    await manager.set_state("other-partition", "QUEUED")
    await manager.set_details(
        "other-partition",
        file_id="file-2",
        partition="tenant-b",
        metadata={},
        user_id=1,
    )

    expected = {"queued-without-details": PENDING_TASK_DETAILS}

    assert await manager.get_matching_active_task_refs(partition="tenant-a", file_id="file-1") == expected
    assert await manager.get_matching_active_task_refs_v2(partition="tenant-a", file_id="file-1") == expected


@pytest.mark.asyncio
async def test_file_delete_fence_rejects_matching_queued_details() -> None:
    manager = _task_state_manager()

    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")
    accepted = await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"filename": "report.txt"},
        user_id=42,
    )

    assert accepted is False
    assert await manager.get_state("task-1") == "CANCELLED"
    assert await manager.get_details("task-1") == {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"filename": "report.txt"},
        "user_id": 42,
    }


@pytest.mark.asyncio
async def test_file_delete_fence_rejects_late_object_ref_registration() -> None:
    manager = _task_state_manager()

    assert await manager.set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=None,
    )
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")

    assert await manager.set_object_ref("task-1", {"ref": object()}) is False
    assert await manager.get_state("task-1") == "CANCELLED"


@pytest.mark.asyncio
async def test_file_delete_fence_only_blocks_same_partition_and_file() -> None:
    manager = _task_state_manager()

    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")

    assert await manager.set_queued_details(
        "other-file",
        file_id="file-2",
        partition="tenant-a",
        metadata={},
        user_id=None,
    )
    assert await manager.set_queued_details(
        "other-partition",
        file_id="file-1",
        partition="tenant-b",
        metadata={},
        user_id=None,
    )

    assert await manager.get_state("other-file") == "QUEUED"
    assert await manager.get_state("other-partition") == "QUEUED"


@pytest.mark.asyncio
async def test_file_delete_fence_is_counted_for_overlapping_deletes() -> None:
    manager = _task_state_manager()

    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")
    await manager.begin_file_delete(partition="tenant-a", file_id="file-1")
    await manager.end_file_delete(partition="tenant-a", file_id="file-1")

    assert (
        await manager.set_queued_details(
            "task-1",
            file_id="file-1",
            partition="tenant-a",
            metadata={},
            user_id=None,
        )
        is False
    )

    await manager.end_file_delete(partition="tenant-a", file_id="file-1")

    assert (
        await manager.set_queued_details(
            "task-2",
            file_id="file-1",
            partition="tenant-a",
            metadata={},
            user_id=None,
        )
        is True
    )


_ACTOR = task_state_module.TaskStateManager.__ray_metadata__.modified_class


def _manager():
    return _ACTOR()


async def _dispatch(mgr, task_id: str, user_id: int = 1) -> None:
    await mgr.set_state(task_id, "QUEUED")
    await mgr.set_details(task_id, file_id=f"f-{task_id}", partition="p", metadata={}, user_id=user_id)


async def test_terminal_tasks_are_evicted_beyond_the_cap(monkeypatch):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 3)
    mgr = _manager()

    for i in range(6):
        await _dispatch(mgr, f"t{i}")
        await mgr.set_state(f"t{i}", "COMPLETED")

    assert len(mgr.tasks) == 3
    # oldest evicted first (FIFO)
    assert set(mgr.tasks) == {"t3", "t4", "t5"}
    assert await mgr.get_state("t0") is None


async def test_eviction_drops_the_user_index_entry_too(monkeypatch):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 1)
    mgr = _manager()

    await _dispatch(mgr, "t0", user_id=7)
    await mgr.set_state("t0", "COMPLETED")
    await _dispatch(mgr, "t1", user_id=7)
    await mgr.set_state("t1", "COMPLETED")

    assert list(mgr.tasks) == ["t1"]
    assert mgr.user_index.get(7) == {"t1"}


async def test_terminal_tasks_are_evicted_once_older_than_the_ttl(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(task_state_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(task_state_module, "_TERMINAL_TTL_SECONDS", 10.0)
    mgr = _manager()

    await _dispatch(mgr, "old")
    await mgr.set_state("old", "COMPLETED")

    clock["now"] = 100.0
    await _dispatch(mgr, "new")
    await mgr.set_state("new", "FAILED")

    assert "old" not in mgr.tasks
    assert "new" in mgr.tasks


async def test_in_flight_tasks_are_never_evicted(monkeypatch):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 1)
    mgr = _manager()

    await _dispatch(mgr, "running")
    await mgr.set_state("running", "SERIALIZING")
    for i in range(5):
        await _dispatch(mgr, f"done{i}")
        await mgr.set_state(f"done{i}", "COMPLETED")

    assert await mgr.get_state("running") == "SERIALIZING"


async def test_cancelled_tasks_are_evictable(monkeypatch):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 0)
    mgr = _manager()

    await _dispatch(mgr, "t0")
    await mgr.set_state("t0", "CANCELLED")

    assert mgr.tasks == {}


async def test_a_claimed_cancellation_is_evictable(monkeypatch):
    """The *real* cancel entry point must register the terminal transition.

    ``test_cancelled_tasks_are_evictable`` above goes through ``set_state``,
    which no cancellation actually uses: ``WorkerDispatcher.cancel_task`` claims
    the cancellation with ``set_cancelled_if_active``, and nothing writes that
    task's state again -- ``ray.cancel`` raises ``CancelledError``, a
    ``BaseException`` that ``process_file``'s ``except Exception`` sails past.
    If this path skips ``_mark_terminal`` the entry never enters
    ``terminal_at``, so neither the cap nor the TTL can reclaim it, and every
    user-initiated cancel leaks one ``TaskInfo`` -- with its details, its
    ``user_index`` entry, and its pinned ``object_ref`` -- for the lifetime of
    the detached actor.
    """
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 0)
    mgr = _manager()

    await _dispatch(mgr, "t0")
    assert await mgr.set_cancelled_if_active("t0") is True

    assert mgr.tasks == {}
    assert mgr.terminal_at == {}
    assert mgr.user_index == {}


async def test_set_error_truncates_long_tracebacks():
    mgr = _manager()
    await _dispatch(mgr, "t0")

    await mgr.set_error("t0", "x" * 100_000)

    stored = await mgr.get_error("t0")
    assert len(stored) <= task_state_module._MAX_ERROR_CHARS + 100
    assert "truncated" in stored


async def test_set_failed_if_not_cancelled_truncates_and_reports_terminality():
    mgr = _manager()
    await _dispatch(mgr, "t0")

    assert await mgr.set_failed_if_not_cancelled("t0", "y" * 100_000) is True
    assert await mgr.get_state("t0") == "FAILED"
    assert len(await mgr.get_error("t0")) <= task_state_module._MAX_ERROR_CHARS + 100


async def test_set_failed_if_not_cancelled_keeps_cancelled_state():
    mgr = _manager()
    await _dispatch(mgr, "t0")
    await mgr.set_state("t0", "CANCELLED")

    assert await mgr.set_failed_if_not_cancelled("t0", "boom") is False
    assert await mgr.get_state("t0") == "CANCELLED"


@pytest.mark.parametrize("state", ["COMPLETED", "FAILED", "CANCELLED"])
async def test_failing_after_eviction_does_not_resurrect_the_task(monkeypatch, state):
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 0)
    mgr = _manager()

    await _dispatch(mgr, "t0")
    await mgr.set_state("t0", state)

    assert await mgr.set_failed_if_not_cancelled("t0", "late") is False
    assert mgr.tasks == {}


def _late_writes():
    """Every setter except ``set_state``, which alone may create a task."""
    return {
        "set_error": lambda mgr: mgr.set_error("t0", "late"),
        "set_details": lambda mgr: mgr.set_details("t0", file_id="f", partition="p", metadata={}, user_id=7),
        "set_object_ref": lambda mgr: mgr.set_object_ref("t0", {"ref": object()}),
    }


@pytest.mark.parametrize("writer", list(_late_writes()))
async def test_a_late_write_does_not_resurrect_an_evicted_task(monkeypatch, writer):
    """A write for an evicted task is dropped, not turned into a new entry.

    Regression: these setters used to go through ``_ensure_task``, which
    recreates on miss. The recreated ``TaskInfo`` has ``state=None``, so it never
    enters ``terminal_at`` and is never evictable again — an unbounded leak on a
    detached actor, which is the whole failure #660 exists to fix.
    """
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 0)
    mgr = _manager()

    await _dispatch(mgr, "t0", user_id=7)
    await mgr.set_state("t0", "COMPLETED")
    assert mgr.tasks == {}, "precondition: the task is evicted"

    await _late_writes()[writer](mgr)

    assert mgr.tasks == {}, f"{writer} resurrected an evicted task"
    assert mgr.terminal_at == {}
    assert mgr.user_index == {}


async def test_a_late_write_for_an_unknown_task_is_dropped():
    """The same guard, without eviction: an id we never dispatched stays unknown."""
    mgr = _manager()

    await mgr.set_error("never-dispatched", "boom")

    assert mgr.tasks == {}
    assert await mgr.get_state("never-dispatched") is None


async def test_the_shipped_bounds_are_the_documented_ones():
    """Pin the production values; every other test here monkeypatches them.

    Without this, narrowing the cap to something that no longer bounds memory —
    or widening it back to the unbounded behaviour of #660 — breaks no test.
    """
    assert task_state_module._MAX_TERMINAL_TASKS == 2000
    assert task_state_module._TERMINAL_TTL_SECONDS == 3600.0


async def test_an_idle_deployment_keeps_its_last_terminal_task_cached(monkeypatch):
    """The TTL is swept lazily, on settle — not on a timer. Reads do not check age.

    This pins the documented trade-off rather than an aspiration: with no new
    task settling, an expired entry stays cached. That is harmless (a terminal
    state is immutable, so a stale read is not a wrong read) and the cap still
    bounds memory — but a reader must not assume the TTL has retired anything.
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(task_state_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(task_state_module, "_TERMINAL_TTL_SECONDS", 10.0)
    mgr = _manager()

    await _dispatch(mgr, "only")
    await mgr.set_state("only", "COMPLETED")

    clock["now"] = 10_000.0  # long past the TTL, but nothing else settles

    assert await mgr.get_state("only") == "COMPLETED"
    assert "only" in mgr.tasks


async def test_a_failure_is_evictable(monkeypatch):
    """The *primary* failure path must register the terminal transition too.

    ``test_a_claimed_cancellation_is_evictable`` pins this for the cancel
    entry point, but the argument applies harder here:
    ``IndexerWorker.process_file``'s ``except`` reaches for
    ``set_failed_if_not_cancelled``, never ``set_state``, so this is the path
    every failed indexing job actually takes. Skipping ``_mark_terminal``
    keeps the entry out of ``terminal_at``, so neither the cap nor the TTL
    can reclaim it and every failure leaks one ``TaskInfo`` -- with its
    stored traceback, its ``user_index`` entry and its pinned
    ``object_ref`` -- for the lifetime of the detached actor. That is
    verbatim the unbounded growth #660 exists to fix.
    """
    monkeypatch.setattr(task_state_module, "_MAX_TERMINAL_TASKS", 0)
    mgr = _manager()

    await _dispatch(mgr, "t0")
    assert await mgr.set_failed_if_not_cancelled("t0", "boom") is True

    assert mgr.tasks == {}
    assert mgr.terminal_at == {}
    assert mgr.user_index == {}
