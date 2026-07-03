"""NATS JetStream backend for :class:`core.ports.task_queue.TaskQueue`.

Push-based work distribution (no SELECT-poll, no shared Postgres): tasks flow
through a JetStream work-queue stream; results flow through a JetStream KV
bucket the producer *watches* (push, not poll). Mechanics behind the port:

    submit()   -> js.publish(task.<topic>) with Nats-Msg-Id = task_id (dedup)
    run()      -> durable pull consumer per topic; N fetch loops share it
    result()   -> KV watch on the task_id key until a terminal status
    retry      -> handler raises → msg.nak() until per-task max_attempts
                  (enforced via num_delivered), then msg.term() + FAILED
    lease      -> consumer ack_wait re-delivers a task whose worker died
    idempotency-> Nats-Msg-Id header + stream duplicate_window

Swappable for Redis/Temporal via the same port — all three pass
``tests/support/task_queue_contract.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import nats
import nats.errors
from nats.js.api import KeyValueConfig, RetentionPolicy, StreamConfig

from core.ports.task_queue import Handler, Task, TaskHandle, TaskQueue, TaskResult, TaskStatus
from core.utils.logging import get_logger

logger = get_logger()

_ACK_WAIT = 900  # seconds: lease before an un-acked (crashed-worker) task redelivers
_DEDUP_WINDOW = 120  # seconds: idempotency dedup window on the stream


class _NatsHandle(TaskHandle):
    def __init__(self, task_id: str, queue: NatsJetStreamTaskQueue) -> None:
        self.task_id = task_id
        self._queue = queue

    async def result(self, timeout: float | None = None) -> TaskResult:
        try:
            return await asyncio.wait_for(self._queue._await_result(self.task_id), timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"task {self.task_id} timed out after {timeout}s") from exc


class NatsJetStreamTaskQueue(TaskQueue):
    def __init__(self, servers: str | list[str] = "nats://localhost:4222", *, namespace: str = "openrag") -> None:
        self._servers = servers
        self._ns = namespace
        self._stream = f"TASKS_{namespace}"
        self._bucket = f"RESULTS_{namespace}"
        self._prefix = f"{namespace}.task"
        self._nc: nats.NATS | None = None
        self._js = None
        self._kv = None
        self._handlers: dict[str, Handler] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._nc is not None:
            return
        async with self._lock:
            if self._nc is not None:  # another coroutine connected while we waited
                return
            await self._connect()

    async def _connect(self):
        self._nc = await nats.connect(self._servers)
        self._js = self._nc.jetstream()
        try:
            await self._js.add_stream(
                StreamConfig(
                    name=self._stream,
                    subjects=[f"{self._prefix}.>"],
                    retention=RetentionPolicy.WORK_QUEUE,
                    duplicate_window=_DEDUP_WINDOW,
                )
            )
        except Exception:  # noqa: BLE001 — already exists with matching config
            pass
        try:
            self._kv = await self._js.key_value(self._bucket)
        except Exception:  # noqa: BLE001 — not created yet
            self._kv = await self._js.create_key_value(KeyValueConfig(bucket=self._bucket))

    # ---- Producer ------------------------------------------------------------
    async def submit(
        self, topic: str, payload: dict[str, Any], *,
        idempotency_key: str | None = None, max_attempts: int = 3,
    ) -> TaskHandle:
        await self._ensure()
        task_id = idempotency_key or uuid.uuid4().hex
        await self._js.publish(
            f"{self._prefix}.{topic}",
            json.dumps(payload).encode(),
            headers={
                "Nats-Msg-Id": task_id,  # stream dedup → idempotency
                "Task-Id": task_id,
                "Max-Attempts": str(max_attempts),
            },
        )
        return _NatsHandle(task_id, self)

    async def get_result(self, task_id: str) -> TaskResult | None:
        await self._ensure()
        try:
            entry = await self._kv.get(task_id)
        except Exception:  # noqa: BLE001 — key-not-found
            return None
        if entry is None or entry.value is None:
            return None
        return _decode_result(entry.value)

    async def _await_result(self, task_id: str) -> TaskResult:
        await self._ensure()
        watcher = await self._kv.watch(task_id)
        try:
            async for entry in watcher:
                if entry is None or entry.value is None:  # init/end marker or delete
                    continue
                res = _decode_result(entry.value)
                if res.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED):
                    return res
        finally:
            await watcher.stop()
        raise asyncio.CancelledError  # watcher ended without a terminal result

    async def _put_result(self, task_id: str, status: TaskStatus, *, result=None, error=None) -> None:
        payload = json.dumps({"task_id": task_id, "status": status.value, "result": result, "error": error})
        await self._kv.put(task_id, payload.encode())

    # ---- Consumer ------------------------------------------------------------
    def register(self, topic: str, handler: Handler) -> None:
        self._handlers[topic] = handler

    async def run(self, *, concurrency: int = 1) -> None:
        await self._ensure()
        for topic in self._handlers:
            durable = f"{self._ns}_worker_{topic}".replace(".", "_")
            subject = f"{self._prefix}.{topic}"
            for _ in range(concurrency):
                self._tasks.append(asyncio.create_task(self._consume(subject, durable)))
        await asyncio.gather(*self._tasks)

    async def _consume(self, subject: str, durable: str) -> None:
        psub = await self._js.pull_subscribe(subject, durable=durable, stream=self._stream)
        while True:
            try:
                msgs = await psub.fetch(1, timeout=2)
            except (nats.errors.TimeoutError, asyncio.TimeoutError):
                continue
            for msg in msgs:
                await self._handle(subject, msg)

    async def _handle(self, subject: str, msg) -> None:
        headers = msg.headers or {}
        task_id = headers.get("Task-Id", "")
        max_attempts = int(headers.get("Max-Attempts", "3"))
        topic = subject[len(self._prefix) + 1:]
        task = Task(id=task_id, topic=topic, payload=json.loads(msg.data.decode()), max_attempts=max_attempts)
        handler = self._handlers[topic]
        try:
            result = await handler(task)
            await self._put_result(task_id, TaskStatus.SUCCEEDED, result=result)
            await msg.ack()
        except Exception as exc:  # noqa: BLE001 — any handler failure becomes task state
            num_delivered = msg.metadata.num_delivered
            if num_delivered >= max_attempts:
                await self._put_result(task_id, TaskStatus.FAILED, error=str(exc))
                await msg.term()  # stop redelivery
            else:
                await msg.nak()  # redeliver

    async def aclose(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None


def _decode_result(raw: bytes) -> TaskResult:
    d = json.loads(raw.decode())
    return TaskResult(task_id=d["task_id"], status=TaskStatus(d["status"]), result=d.get("result"), error=d.get("error"))


__all__ = ["NatsJetStreamTaskQueue"]
