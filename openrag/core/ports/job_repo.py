"""Job repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openrag.core.models.catalog import IndexationJob


class JobRepository(ABC):
    """CRUD operations for indexation jobs."""

    @abstractmethod
    async def create_job(self, job: IndexationJob) -> IndexationJob: ...

    @abstractmethod
    async def get_job(self, job_id: str) -> IndexationJob | None: ...

    @abstractmethod
    async def list_jobs(self, status: str | None = None, offset: int = 0, limit: int = 50) -> list[IndexationJob]: ...

    @abstractmethod
    async def update_job(self, job_id: str, **fields: Any) -> IndexationJob | None: ...
