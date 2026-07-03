"""Postgres-as-queue backend for :class:`core.ports.task_queue.TaskQueue`.

Uses ``SELECT … FOR UPDATE SKIP LOCKED`` for lock-free concurrent claiming and a
``locked_until`` lease so a task whose worker dies is re-claimed after the lease
expires (at-least-once delivery; handlers must be idempotent — enforced upstream
by ``idempotency_key``).

Ships now; swap for ``ArqTaskQueue`` / ``TemporalTaskQueue`` later by changing the
DI wiring only (see the port docstring). Nothing in the marker-serve worker or the
marker_client adapter references this class directly — they depend on ``TaskQueue``.

Table (add via an idempotent Alembic migration, per the repo's create_all+alembic
convention — guard with ``table_exists``):

    CREATE TABLE IF NOT EXISTS parse_tasks (
        id              TEXT PRIMARY KEY,
        topic           TEXT NOT NULL,
        payload         JSONB NOT NULL,
        status          TEXT NOT NULL DEFAULT 'PENDING',
        idempotency_key TEXT UNIQUE,
        attempts        INT  NOT NULL DEFAULT 0,
        max_attempts    INT  NOT NULL DEFAULT 3,
        result          JSONB,
        error           TEXT,
        locked_until    TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    -- claim hot path:
    CREATE INDEX IF NOT EXISTS ix_parse_tasks_claim
        ON parse_tasks (topic, status, locked_until) WHERE status IN ('PENDING','RUNNING');
    -- KEDA Postgres scaler counts pending work with:
    --   SELECT count(*) FROM parse_tasks WHERE status='PENDING';
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from core.ports.task_queue import Handler, Task, TaskHandle, TaskQueue, TaskResult, TaskStatus
from core.utils.logging import get_logger
from sqlalchemy import text

logger = get_logger()

# Injected: a zero-arg callable returning an AsyncSession context manager,
# i.e. the app's existing async_sessionmaker (see services/persistence wiring).
SessionFactory = Callable[[], Any]

_LEASE_SECONDS = 900  # re-claimable if a worker dies mid-parse; renewed by heartbeat


class _PgTaskHandle(TaskHandle):
    def __init__(self, task_id: str, queue: PgTaskQueue) -> None:
        self.task_id = task_id
        self._queue = queue

    async def result(self, timeout: float | None = None, poll_interval: float = 1.0) -> TaskResult:
        waited = 0.0
        while True:
            res = await self._queue.get_result(self.task_id)
            if res and res.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED):
                return res
            if timeout is not None and waited >= timeout:
                raise TimeoutError(f"task {self.task_id} timed out after {timeout}s")
            await asyncio.sleep(poll_interval)
            waited += poll_interval


class PgTaskQueue(TaskQueue):
    def __init__(self, session_factory: SessionFactory, *, poll_idle: float = 1.0) -> None:
        self._session = session_factory
        self._poll_idle = poll_idle
        self._handlers: dict[str, Handler] = {}

    # ---- Producer ------------------------------------------------------------
    async def submit(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> TaskHandle:
        task = Task(topic=topic, payload=payload, idempotency_key=idempotency_key, max_attempts=max_attempts)
        async with self._session() as s:
            # ON CONFLICT on the unique idempotency_key makes retries return the
            # existing task instead of enqueuing a duplicate.
            row = (
                await s.execute(
                    text("""
                INSERT INTO parse_tasks (id, topic, payload, status, idempotency_key, max_attempts)
                VALUES (:id, :topic, :payload, 'PENDING', :ikey, :maxa)
                ON CONFLICT (idempotency_key) DO UPDATE SET updated_at = now()
                RETURNING id
            """),
                    {
                        "id": task.id,
                        "topic": topic,
                        "payload": _json(payload),
                        "ikey": idempotency_key,
                        "maxa": max_attempts,
                    },
                )
            ).scalar_one()
            await s.commit()
        return _PgTaskHandle(row, self)

    async def get_result(self, task_id: str) -> TaskResult | None:
        async with self._session() as s:
            row = (
                await s.execute(text("SELECT status, result, error FROM parse_tasks WHERE id = :id"), {"id": task_id})
            ).first()
        if row is None:
            return None
        return TaskResult(task_id=task_id, status=TaskStatus(row.status), result=row.result, error=row.error)

    # ---- Consumer ------------------------------------------------------------
    def register(self, topic: str, handler: Handler) -> None:
        self._handlers[topic] = handler

    async def run(self, *, concurrency: int = 1) -> None:
        sem = asyncio.Semaphore(concurrency)

        async def worker_loop() -> None:
            while True:
                task = await self._claim(list(self._handlers.keys()))
                if task is None:
                    await asyncio.sleep(self._poll_idle)
                    continue
                async with sem:
                    await self._execute(task)

        await asyncio.gather(*[worker_loop() for _ in range(concurrency)])

    async def _claim(self, topics: list[str]) -> Task | None:
        """Atomically claim one PENDING (or lease-expired RUNNING) task."""
        async with self._session() as s:
            row = (
                await s.execute(
                    text(f"""
                UPDATE parse_tasks SET
                    status = 'RUNNING',
                    attempts = attempts + 1,
                    locked_until = now() + interval '{_LEASE_SECONDS} seconds',
                    updated_at = now()
                WHERE id = (
                    SELECT id FROM parse_tasks
                    WHERE topic = ANY(:topics)
                      AND (status = 'PENDING' OR (status = 'RUNNING' AND locked_until < now()))
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, topic, payload, attempts, max_attempts
            """),
                    {"topics": topics},
                )
            ).first()
            await s.commit()
        if row is None:
            return None
        return Task(
            id=row.id, topic=row.topic, payload=row.payload, attempts=row.attempts, max_attempts=row.max_attempts
        )

    async def _execute(self, task: Task) -> None:
        handler = self._handlers[task.topic]
        hb = asyncio.create_task(self._heartbeat(task.id))
        try:
            result = await handler(task)
            await self._finish(task.id, TaskStatus.SUCCEEDED, result=result)
        except Exception as exc:  # noqa: BLE001 — turn any handler failure into task state
            logger.exception("parse task failed", task_id=task.id, topic=task.topic)
            if task.attempts >= task.max_attempts:
                await self._finish(task.id, TaskStatus.FAILED, error=str(exc))
            else:
                await self._requeue(task.id)  # back to PENDING for another worker
        finally:
            hb.cancel()

    async def _heartbeat(self, task_id: str) -> None:
        """Renew the lease while the handler runs (long PDFs), so it isn't reclaimed."""
        try:
            while True:
                await asyncio.sleep(_LEASE_SECONDS / 3)
                async with self._session() as s:
                    await s.execute(
                        text(
                            f"UPDATE parse_tasks SET locked_until = now() + interval '{_LEASE_SECONDS} seconds' "
                            "WHERE id = :id AND status = 'RUNNING'"
                        ),
                        {"id": task_id},
                    )
                    await s.commit()
        except asyncio.CancelledError:
            pass

    async def _finish(self, task_id: str, status: TaskStatus, *, result=None, error=None) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "UPDATE parse_tasks SET status=:st, result=:res, error=:err, "
                    "locked_until=NULL, updated_at=now() WHERE id=:id"
                ),
                {"st": status.value, "res": _json(result) if result else None, "err": error, "id": task_id},
            )
            await s.commit()

    async def _requeue(self, task_id: str) -> None:
        async with self._session() as s:
            await s.execute(
                text("UPDATE parse_tasks SET status='PENDING', locked_until=NULL, updated_at=now() WHERE id=:id"),
                {"id": task_id},
            )
            await s.commit()


def _json(value: Any) -> Any:
    import json

    return json.dumps(value)


__all__ = ["PgTaskQueue"]
