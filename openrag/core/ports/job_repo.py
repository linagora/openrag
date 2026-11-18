"""Job repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from core.models.catalog import IndexationJob
from core.models.stage_progress import (
    DurableStageProgress,
    StageProgressEvent,
    StageProgressEventCursor,
    StageProgressEventPage,
    StageProgressSummary,
    StageProgressWriteResult,
)


class JobListSnapshot(Protocol):
    """Consistent durable reads used to build one paginated task-list response."""

    async def get_job_list_metadata_with_actor_rows(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        actor_task_ids: list[str],
    ) -> tuple[int, dict[str, int], dict[str, tuple[str, str | None]]]: ...

    async def list_task_page_by_stage_order(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        actor_overlays: list[tuple[str, str | None, datetime | None]],
        offset: int = 0,
        limit: int = 50,
    ) -> list[tuple[str, IndexationJob | None]]: ...


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
        limit: int | None = 50,
    ) -> list[IndexationJob]: ...

    @abstractmethod
    async def list_jobs_by_stage_order(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        exclude_task_ids: list[str] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[IndexationJob]:
        """Return one globally stage-ordered task page without loading history."""

    @abstractmethod
    async def list_task_page_by_stage_order(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        actor_overlays: list[tuple[str, str | None, datetime | None]],
        offset: int = 0,
        limit: int = 50,
    ) -> list[tuple[str, IndexationJob | None]]:
        """Return a globally ordered page, merging actor-only/newer states in SQL."""

    @abstractmethod
    async def get_job_list_metadata(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
    ) -> tuple[int, dict[str, int]]:
        """Return the matching durable count and per-partition counts."""

    @abstractmethod
    async def get_job_list_metadata_with_actor_rows(
        self,
        *,
        statuses: list[str] | None = None,
        user_id: int | None = None,
        actor_task_ids: list[str],
    ) -> tuple[int, dict[str, int], dict[str, tuple[str, str | None]]]:
        """Read list counts and matching actor task states in one snapshot."""

    @abstractmethod
    def task_list_snapshot(self) -> AbstractAsyncContextManager[JobListSnapshot]:
        """Open one repeatable-read snapshot for task-list metadata and page reads."""

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
    async def write_stage_progress(
        self,
        *,
        events: Sequence[StageProgressEvent],
        summaries: Sequence[StageProgressSummary],
    ) -> StageProgressWriteResult:
        """Persist bounded, idempotent stage observations and their summary."""

    @abstractmethod
    async def get_stage_progress_summaries(self, task_ids: list[str]) -> dict[str, DurableStageProgress]:
        """Bulk-read compact stage summaries without loading event histories."""

    @abstractmethod
    async def mark_stage_progress_interrupted(self, task_id: str) -> bool:
        """Record confirmed settlement and mark an open stage now or when its snapshot arrives."""

    @abstractmethod
    async def get_latest_stage_ids(self, task_ids: list[str]) -> dict[str, str]:
        """Return the stage ID from the newest persisted event for each task."""

    @abstractmethod
    async def get_stage_progress_events(
        self,
        task_id: str,
        *,
        before: StageProgressEventCursor | None = None,
        limit: int = 100,
    ) -> StageProgressEventPage:
        """Read a bounded page of one task's history in execution order."""

    @abstractmethod
    async def fail_orphaned_jobs(self, *, active_ids: list[str], error: str, before: datetime) -> int:
        """Fail unfinished records no live task owns, e.g. after a restart."""

    @abstractmethod
    async def purge_terminal_jobs(self, *, older_than: datetime) -> int:
        """Drop finished records past retention so the table stays bounded."""
