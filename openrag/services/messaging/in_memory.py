"""In-process :class:`TaskQueue` — reference implementation.

Purposes:
  * the conformance suite's self-check (proves the contract tests are correct),
  * a fast test double for anything that depends on ``TaskQueue``,
  * a single-process dev backend (NOT for cross-container use — the single-server
    compose mode runs marker-serve as a separate process and needs a real broker).

Delivery is at-least-once via retry: a handler that raises is redelivered until
``max_attempts``, then the task is marked FAILED. Results are pushed via an
``asyncio.Event`` (no polling), mirroring how a broker delivers results.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.ports.task_queue import Handler, Task, TaskHandle, TaskQueue, TaskResult, TaskStatus


class _InMemoryHandle(TaskHandle):
    def __init__(self, task_id: str, queue: InMemoryTaskQueue) -> None:
        self.task_id = task_id
        self._queue = queue

    async def result(self, timeout: float | None = None) -> TaskResult:
        event = self._queue._events[self.task_id]
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"task {self.task_id} timed out after {timeout}s") from exc
        return self._queue._results[self.task_id]


class InMemoryTaskQueue(TaskQueue):
    def __init__(self) -> None:
        self._pending: asyncio.Queue[Task] = asyncio.Queue()
        self._results: dict[str, TaskResult] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._by_idem: dict[str, str] = {}
        self._handlers: dict[str, Handler] = {}
        self._workers: list[asyncio.Task[None]] = []

    # ---- Producer ------------------------------------------------------------
    async def submit(
        self, topic: str, payload: dict[str, Any], *,
        idempotency_key: str | None = None, max_attempts: int = 3,
    ) -> TaskHandle:
        if idempotency_key is not None and idempotency_key in self._by_idem:
            return _InMemoryHandle(self._by_idem[idempotency_key], self)
        task = Task(topic=topic, payload=payload, idempotency_key=idempotency_key, max_attempts=max_attempts)
        self._events[task.id] = asyncio.Event()
        if idempotency_key is not None:
            self._by_idem[idempotency_key] = task.id
        await self._pending.put(task)
        return _InMemoryHandle(task.id, self)

    async def get_result(self, task_id: str) -> TaskResult | None:
        return self._results.get(task_id)

    # ---- Consumer ------------------------------------------------------------
    def register(self, topic: str, handler: Handler) -> None:
        self._handlers[topic] = handler

    async def run(self, *, concurrency: int = 1) -> None:
        self._workers = [asyncio.create_task(self._worker()) for _ in range(concurrency)]
        try:
            await asyncio.gather(*self._workers)
        except asyncio.CancelledError:
            for w in self._workers:
                w.cancel()
            raise

    async def _worker(self) -> None:
        while True:
            task = await self._pending.get()
            await self._execute(task)

    async def _execute(self, task: Task) -> None:
        task.attempts += 1
        handler = self._handlers.get(task.topic)
        if handler is None:
            self._finish(task, TaskStatus.FAILED, error=f"no handler for topic {task.topic!r}")
            return
        try:
            result = await handler(task)
            self._finish(task, TaskStatus.SUCCEEDED, result=result)
        except Exception as exc:  # noqa: BLE001 — any handler failure becomes task state
            if task.attempts >= task.max_attempts:
                self._finish(task, TaskStatus.FAILED, error=str(exc))
            else:
                await self._pending.put(task)  # redeliver (attempts carried on the Task)

    def _finish(self, task: Task, status: TaskStatus, *, result=None, error=None) -> None:
        self._results[task.id] = TaskResult(task_id=task.id, status=status, result=result, error=error)
        self._events[task.id].set()

    async def aclose(self) -> None:
        for w in self._workers:
            w.cancel()


__all__ = ["InMemoryTaskQueue"]
