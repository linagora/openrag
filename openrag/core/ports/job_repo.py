from abc import ABC, abstractmethod


class JobRepo(ABC):
    """Port for indexing job/task tracking.

    Provisional — currently job tracking uses Ray's TaskStateManager actor.
    This port defines the contract for a persistence-backed job store
    that Phase 2+ will implement.
    """

    @abstractmethod
    async def create_job(
        self,
        job_id: str,
        partition: str,
        file_id: str,
        user_id: int | None = None,
    ) -> dict:
        """Create a new indexing job record."""
        ...

    @abstractmethod
    async def update_job_status(
        self,
        job_id: str,
        status: str,
        metadata: dict | None = None,
    ) -> None:
        """Update a job's status and optional metadata."""
        ...

    @abstractmethod
    async def get_job(self, job_id: str) -> dict | None:
        """Get a job by ID. Returns None if not found."""
        ...

    @abstractmethod
    async def list_jobs(
        self,
        partition: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """List jobs, optionally filtered by partition and/or status."""
        ...
