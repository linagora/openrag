"""Stub :class:`JobRepository` — see ``_stubs.py`` for the rationale.

Job state is currently tracked in-memory by the
:class:`components.indexer.indexer.TaskStateManager` Ray actor. The
post-refactoring P0 feature is to persist jobs to Postgres so they
survive restarts and become visible to operators. When that lands,
swap the body of each method for an asyncpg implementation against a
new ``jobs`` table — the port shape is already pinned by Phase 4.
"""

from __future__ import annotations

from typing import Any

from core.models.catalog import IndexationJob
from core.ports.job_repo import JobRepository
from services.persistence._stubs import _StubRepositoryBase, stub_not_implemented


class PgJobRepository(_StubRepositoryBase, JobRepository):
    """TODO: real impl once the ``jobs`` table is added. See REFACTORING P0 plan."""

    async def create_job(self, job: IndexationJob) -> IndexationJob:
        raise stub_not_implemented("DB-backed job tracking")

    async def get_job(self, job_id: str) -> IndexationJob | None:
        raise stub_not_implemented("DB-backed job tracking")

    async def list_jobs(
        self,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[IndexationJob]:
        raise stub_not_implemented("DB-backed job tracking")

    async def update_job(self, job_id: str, **fields: Any) -> IndexationJob | None:
        raise stub_not_implemented("DB-backed job tracking")


__all__ = ["PgJobRepository"]
