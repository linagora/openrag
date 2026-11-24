"""Workspace repository interface — workspaces + workspace_files join."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from datetime import datetime

from core.models.workspace import Workspace


class WorkspaceRepository(ABC):
    """CRUD operations for workspaces and their file membership.

    A workspace is a named subset of files within a partition. The actual
    file contents are not copied — the join table ``workspace_files``
    references the canonical ``files`` row by integer PK so file deletion
    cascades correctly.

    ``workspace_id`` is unique per partition, not globally: the same id may
    exist in several partitions as distinct workspaces. Every method that
    addresses one workspace therefore takes ``(partition, workspace_id)``;
    :meth:`find_workspaces` is the only id-only lookup and returns every
    match so the caller can detect ambiguity.
    """

    # ── Workspace lifecycle ───────────────────────────────────────────

    @abstractmethod
    def cleanup_session(self, file_id: str, partition: str) -> AbstractAsyncContextManager[WorkspaceRepository | None]:
        """Yield an exclusively owned cleanup repository, or None if busy.

        Cleanup transitions must use this session. Ownership lasts through
        vector deletion and database finalization, independently of claim age.
        """
        ...

    @abstractmethod
    async def create_workspace(self, workspace: Workspace) -> Workspace: ...

    @abstractmethod
    async def get_workspace(self, partition: str, workspace_id: str) -> Workspace | None: ...

    @abstractmethod
    async def find_workspaces(self, workspace_id: str, partitions: list[str] | None) -> list[Workspace]:
        """Every workspace called ``workspace_id`` in ``partitions`` (``None`` = any partition)."""
        ...

    @abstractmethod
    async def list_workspaces(self, partition: str) -> list[Workspace]: ...

    @abstractmethod
    async def delete_workspace(
        self,
        partition: str,
        workspace_id: str,
        *,
        keep_files: bool = False,
        expected_created_at: datetime | None = None,
        expected_created_at_is_null: bool = False,
    ) -> list[str] | None:
        """Delete a workspace and return claimed workspace-owned orphan IDs.

        With keep_files, retain its files as independently indexed instead.
        The returned candidates still describe what would have been deleted.
        Return ``None`` when the workspace no longer matches the generation
        supplied by a confirmation flow.
        """
        ...

    @abstractmethod
    async def finalize_claimed_file_cleanup(self, file_id: str, partition: str) -> bool:
        """Delete a file that was claimed during workspace cleanup."""
        ...

    @abstractmethod
    async def start_claimed_file_cleanup(self, file_id: str, partition: str) -> bool:
        """Mark a cleanup claim as destructive and no longer recoverable."""
        ...

    @abstractmethod
    async def mark_cleanup_failed(self, file_id: str, partition: str) -> bool:
        """Persist that destructive cleanup failed and must be retried."""
        ...

    @abstractmethod
    async def claim_failed_file_cleanup(self, file_id: str, partition: str) -> bool:
        """Atomically reclaim failed or abandoned cleanup before retry."""
        ...

    @abstractmethod
    async def release_claimed_file_cleanup(self, file_id: str, partition: str) -> None:
        """Make a claimed file attachable after cleanup fails."""
        ...

    # ── Workspace ↔ file membership ───────────────────────────────────

    @abstractmethod
    async def add_files_to_workspace(
        self,
        partition: str,
        workspace_id: str,
        file_ids: list[str],
        *,
        expected_created_at: datetime | None = None,
        expected_created_at_is_null: bool = False,
    ) -> list[str] | None:
        """Attach files to a workspace. Returns the file_ids that could not be resolved.

        Returns ``None`` when a generation precondition is supplied and the
        workspace no longer matches it (a same-ID replacement).
        """
        ...

    @abstractmethod
    async def remove_file_from_workspace(
        self,
        partition: str,
        workspace_id: str,
        file_id: str,
        *,
        expected_created_at: datetime | None = None,
        expected_created_at_is_null: bool = False,
    ) -> bool | None:
        """Return None when the workspace generation no longer matches."""
        ...

    @abstractmethod
    async def list_workspace_files(
        self,
        partition: str,
        workspace_id: str,
        *,
        expected_created_at: datetime | None = None,
        expected_created_at_is_null: bool = False,
    ) -> list[str] | None:
        """List membership, returning None when the workspace is missing or stale."""
        ...

    @abstractmethod
    async def get_file_workspaces(self, file_id: str, partition: str) -> list[str]: ...

    @abstractmethod
    async def get_existing_file_ids(self, partition: str, file_ids: list[str]) -> set[str]:
        """Return the subset of ``file_ids`` that actually exist in ``partition``."""
        ...

    @abstractmethod
    async def get_existing_file_ids_any_partition(self, file_ids: list[str]) -> set[str]:
        """Return the subset of ``file_ids`` that exist in *any* partition.

        Unscoped by design — only for the ``SUPER_ADMIN_MODE`` ``"all"`` wildcard.
        """
        ...

    @abstractmethod
    async def remove_file_from_all_workspaces(self, file_id: str, partition: str) -> None:
        """Detach ``file_id`` from every workspace in ``partition``."""
        ...
