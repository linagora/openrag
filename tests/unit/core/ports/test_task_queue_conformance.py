"""Conformance suite for the :class:`TaskQueue` port.

Every backend adapter MUST pass these tests. They pin the contract that the
marker-serve worker and marker_client adapter depend on, so a NATS→Redis→Temporal
swap is provably safe as long as the new backend stays green here.

To add a backend: register a factory in ``_make`` and its id in ``BACKENDS``.
The NATS/Temporal factories are integration-only (need a running server) and
should be added with ``pytest.param(..., marks=pytest.mark.integration)`` plus a
skip-if-unavailable guard, in ``tests/integration``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from core.ports.task_queue import Task, TaskQueue, TaskStatus
from services.messaging.in_memory import InMemoryTaskQueue

BACKENDS = ["in_memory"]


async def _make(name: str) -> TaskQueue:
    if name == "in_memory":
        return InMemoryTaskQueue()
    raise ValueError(f"unknown backend {name!r}")


@pytest.fixture(params=BACKENDS)
async def task_queue(request):
    queue = await _make(request.param)
    try:
        yield queue
    finally:
        await queue.aclose()


@asynccontextmanager
async def consuming(queue: TaskQueue, concurrency: int = 2):
    """Run the consumer in the background for the duration of the block."""
    task = asyncio.create_task(queue.run(concurrency=concurrency))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------- #
async def test_submit_and_result_success(task_queue):
    async def handler(task: Task) -> dict:
        return {"echo": task.payload["x"]}

    task_queue.register("t", handler)
    async with consuming(task_queue):
        handle = await task_queue.submit("t", {"x": 42})
        res = await handle.result(timeout=5)

    assert res.status is TaskStatus.SUCCEEDED
    assert res.result == {"echo": 42}


async def test_get_result_unknown_returns_none(task_queue):
    assert await task_queue.get_result("does-not-exist") is None


async def test_result_timeout_raises(task_queue):
    async def handler(task: Task) -> dict:
        return {}

    task_queue.register("t", handler)
    # No consumer running → the result never arrives.
    handle = await task_queue.submit("t", {})
    with pytest.raises(TimeoutError):
        await handle.result(timeout=0.3)


async def test_idempotency_key_dedups(task_queue):
    calls = 0

    async def handler(task: Task) -> dict:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    task_queue.register("t", handler)
    async with consuming(task_queue):
        h1 = await task_queue.submit("t", {}, idempotency_key="dup")
        h2 = await task_queue.submit("t", {}, idempotency_key="dup")
        assert h1.task_id == h2.task_id
        res = await h1.result(timeout=5)

    assert res.status is TaskStatus.SUCCEEDED
    assert calls == 1  # deduped: the handler ran once


async def test_retry_until_success(task_queue):
    attempts = 0

    async def handler(task: Task) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("transient")
        return {"ok": True}

    task_queue.register("t", handler)
    async with consuming(task_queue, concurrency=1):
        res = await (await task_queue.submit("t", {}, max_attempts=3)).result(timeout=5)

    assert res.status is TaskStatus.SUCCEEDED
    assert attempts == 3


async def test_retry_exhausted_fails(task_queue):
    async def handler(task: Task) -> dict:
        raise RuntimeError("always-fails")

    task_queue.register("t", handler)
    async with consuming(task_queue, concurrency=1):
        res = await (await task_queue.submit("t", {}, max_attempts=2)).result(timeout=5)

    assert res.status is TaskStatus.FAILED
    assert "always-fails" in (res.error or "")


async def test_concurrency_processes_each_task_once(task_queue):
    seen: list[int] = []

    async def handler(task: Task) -> dict:
        await asyncio.sleep(0.01)
        seen.append(task.payload["i"])
        return {"i": task.payload["i"]}

    task_queue.register("t", handler)
    n = 20
    async with consuming(task_queue, concurrency=5):
        handles = [await task_queue.submit("t", {"i": i}) for i in range(n)]
        results = [await h.result(timeout=5) for h in handles]

    assert all(r.status is TaskStatus.SUCCEEDED for r in results)
    assert sorted(seen) == list(range(n))  # each task processed exactly once
