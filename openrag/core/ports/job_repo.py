"""Job repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openrag.core.models.catalog import IndexationJob

# The non-terminal states an indexation job passes through. Shared by the
# repository implementations and the read models so "active"/"pending" means
# exactly one thing across the stack.
ACTIVE_JOB_STATES: tuple[str, ...] = ("QUEUED", "SERIALIZING", "CHUNKING", "INSERTING")
TERMINAL_JOB_STATES: tuple[str, ...] = ("COMPLETED", "FAILED", "CANCELLED")


class JobRepository(ABC):
    """CRUD operations for indexation jobs."""

    @abstractmethod
    async def create_job(self, job: IndexationJob) -> IndexationJob: ...

    @abstractmethod
    async def get_job(self, job_id: str) -> IndexationJob | None: ...

    @abstractmethod
    async def list_jobs(
        self,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
        user_id: int | None = None,
    ) -> list[IndexationJob]:
        """Return jobs newest-first.

        ``status`` accepts an exact state (case-insensitive) or the pseudo-status
        ``"active"``, which expands to :data:`ACTIVE_JOB_STATES`. ``user_id``
        scopes the result to one uploader; ``None`` means every job.
        """

    @abstractmethod
    async def update_job(self, job_id: str, **fields: Any) -> IndexationJob | None: ...

    @abstractmethod
    async def count_by_status(self) -> dict[str, int]:
        """Return ``{status: count}`` over every retained job.

        Cheaper than paging the whole table for the queue-info roll-up.
        """

    @abstractmethod
    async def purge_terminal_jobs(self, *, older_than_seconds: int, keep_last: int) -> int:
        """Evict terminal jobs, returning how many rows were removed.

        Retention is what keeps the durable store from repeating the unbounded
        growth of the in-memory actor it replaced. Both bounds apply: a terminal
        job is removed once it is older than ``older_than_seconds`` **or** once
        it falls outside the ``keep_last`` most recently completed rows.
        In-flight jobs are never touched.
        """
