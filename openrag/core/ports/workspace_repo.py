"""Workspace repository interface — workspaces + workspace_files join."""

from __future__ import annotations

from abc import ABC, abstractmethod

from openrag.core.models.workspace import Workspace


class WorkspaceRepository(ABC):
    """CRUD operations for workspaces and their file membership.

    A workspace is a named subset of files within a partition. The actual
    file contents are not copied — the join table ``workspace_files``
    references the canonical ``files`` row by integer PK so file deletion
    cascades correctly.
    """

    # ── Workspace lifecycle ───────────────────────────────────────────

    @abstractmethod
    async def create_workspace(self, workspace: Workspace) -> Workspace: ...

    @abstractmethod
    async def get_workspace(self, workspace_id: str) -> Workspace | None: ...

    @abstractmethod
    async def list_workspaces(self, partition: str) -> list[Workspace]: ...

    @abstractmethod
    async def delete_workspace(self, workspace_id: str) -> list[str]:
        """Delete a workspace, return file_ids that no longer belong to any workspace."""
        ...

    # ── Workspace ↔ file membership ───────────────────────────────────

    @abstractmethod
    async def add_files_to_workspace(self, workspace_id: str, file_ids: list[str]) -> list[str]:
        """Attach files to a workspace. Returns the file_ids that could not be resolved."""
        ...

    @abstractmethod
    async def remove_file_from_workspace(self, workspace_id: str, file_id: str) -> bool: ...

    @abstractmethod
    async def list_workspace_files(self, workspace_id: str) -> list[str]: ...

    @abstractmethod
    async def get_file_workspaces(self, file_id: str, partition: str) -> list[str]: ...

    @abstractmethod
    async def get_existing_file_ids(self, partition: str, file_ids: list[str]) -> set[str]:
        """Return the subset of ``file_ids`` that actually exist in ``partition``."""
        ...

    @abstractmethod
    async def remove_file_from_all_workspaces(self, file_id: str, partition: str) -> None:
        """Detach ``file_id`` from every workspace in ``partition``."""
        ...
