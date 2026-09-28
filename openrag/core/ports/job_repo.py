"""Job repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from core.models.catalog import IndexationJob


class JobRepository(ABC):
    """Durable records for indexing tasks."""

    @abstractmethod
    async def upsert_job(self, job: IndexationJob) -> IndexationJob:
        """Create the record, or advance it. Terminal states never regress."""

    @abstractmethod
    async def get_job(self, job_id: str) -> IndexationJob | None: ...

    @abstractmethod
    async def get_jobs(self, job_ids: list[str]) -> list[IndexationJob]:
        """Return durable rows for the supplied task IDs."""

    @abstractmethod
    async def list_jobs(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[IndexationJob]: ...

    @abstractmethod
    async def count_jobs(self) -> dict[str, int]:
        """Return durable task counts grouped by status."""

    @abstractmethod
    async def get_job_states(
        self,
        *,
        statuses: list[str] | None = None,
        job_ids: list[str] | None = None,
    ) -> dict[str, str]:
        """Return ``{task_id: status}`` for rows matching every given filter.

        At least one filter is required: an unfiltered read would scan the
        whole retention window, which is what this narrow read exists to avoid.
        """

    @abstractmethod
    async def fail_orphaned_jobs(self, *, active_ids: list[str], error: str, before: datetime) -> int:
        """Fail unfinished records no live task owns, e.g. after a restart."""

    @abstractmethod
    async def purge_terminal_jobs(self, *, older_than: datetime) -> int:
        """Drop finished records past retention so the table stays bounded."""
