"""Work-queue port — backend-agnostic async task distribution.

This is the seam that lets a **Postgres-as-queue** backend ship now and a
**Temporal** or **arq** backend drop in later *without touching the producers
(`marker_client`) or consumers (`marker-serve` worker)*. Both sides depend only
on this interface; the concrete backend is chosen in the DI layer.

Distinct from :class:`core.ports.job_repo.JobRepository`, which is
operator-facing *batch-job tracking* CRUD (`IndexationJob`). This port is the
*work-distribution* layer: enqueue a unit of work, have a worker execute it,
deliver the result back. A handler MAY update `IndexationJob`/`DocumentRecord`
status as a side effect, but that stays a separate concern.

Why the ``submit`` / ``register`` + ``run`` shape (and not ``claim``):
Postgres is *pull*-based (workers claim rows via ``SKIP LOCKED``) while
arq/Temporal are *push*-based (the framework dispatches to your handler). A raw
``claim()`` API would leak the pull model and not map onto arq/Temporal. Instead
the consumer registers a **handler** (`Task -> result dict`) and calls ``run()``;
each backend owns the dispatch mechanism behind that identical contract:

    backend    submit()              run() dispatch            handle.result()
    --------   -------------------   -----------------------   --------------------
    Postgres   INSERT row            SKIP LOCKED claim loop     poll result column
    arq        enqueue_job()         arq worker (framework)     Job.result()
    Temporal   start_workflow()      Temporal worker (poll)     WorkflowHandle.result()

So swapping backends = swapping the DI-wired `TaskQueue` impl. The worker and the
client adapter never change.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Task(BaseModel):
    """A unit of work on the queue."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    topic: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskResult(BaseModel):
    task_id: str
    status: TaskStatus
    result: dict[str, Any] | None = None
    error: str | None = None


# A consumer handler: receives the Task, returns the result payload, or raises.
Handler = Callable[[Task], Awaitable[dict[str, Any]]]


class TaskHandle(ABC):
    """Producer-side handle to an in-flight task; how the result is awaited is
    the backend's business (poll for Postgres, native await for arq/Temporal)."""

    task_id: str

    @abstractmethod
    async def result(self, timeout: float | None = None) -> TaskResult:
        """Block until the task finishes or ``timeout`` elapses (raises TimeoutError)."""
        ...


class TaskQueue(ABC):
    """Backend-agnostic async work queue."""

    # ---- Producer side (used by the marker_client adapter) -------------------
    @abstractmethod
    async def submit(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> TaskHandle:
        """Enqueue work. If ``idempotency_key`` matches an existing task, return a
        handle to that one instead of enqueuing a duplicate (safe retries)."""
        ...

    @abstractmethod
    async def get_result(self, task_id: str) -> TaskResult | None:
        """Point-in-time status/result, or None if unknown."""
        ...

    # ---- Consumer side (used by the marker-serve worker) ---------------------
    @abstractmethod
    def register(self, topic: str, handler: Handler) -> None:
        """Bind a handler to a topic. Call before ``run()``."""
        ...

    @abstractmethod
    async def run(self, *, concurrency: int = 1) -> None:
        """Start consuming and dispatching to registered handlers. Blocks."""
        ...


__all__ = ["TaskQueue", "TaskHandle", "Task", "TaskResult", "TaskStatus", "Handler"]
