"""WorkspaceService — workspace CRUD + file association (Phase 8B.2).

Business logic extracted from ``routers/workspaces.py`` and the
workspace slice of the legacy Ray ``vectordb`` shim. The simple
endpoints were already 1:1 repo delegations; the substantive extraction
is :meth:`delete_workspace`, the cross-cutting op that drops the
workspace, then deletes every file orphaned by that removal from *both*
the vector store and the relational catalog (the legacy router looped the Ray vectordb
delete-file call itself).

The thin router keeps the HTTP guards whose exact non-bracketed
``{"detail": ...}`` body must stay identical (409 on duplicate, the
``require_workspace_in_partition`` 404, the unknown/missing-file 404s).

Constructor note: ``collection`` (vector-store collection name) is one
arg beyond the plan's three — the legacy ``delete_file`` read it from
``config.vectordb.collection_name``; the container supplies it from
settings so the service stays Ray/config-free (8H).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.ports.document_repo import DocumentRepository
    from core.ports.workspace_repo import WorkspaceRepository
    from core.vector_stores import VectorStore

logger = get_logger()


class WorkspaceService:
    """Workspace lifecycle, file association and orphan cleanup."""

    def __init__(
        self,
        *,
        workspace_repo: WorkspaceRepository,
        document_repo: DocumentRepository,
        vector_store: VectorStore,
        collection: str,
    ) -> None:
        self._workspace_repo = workspace_repo
        self._document_repo = document_repo
        self._vector_store = vector_store
        self._collection = collection

    # ------------------------------------------------------------------
    # CRUD / lookups (thin repo delegations)
    # ------------------------------------------------------------------

    async def get_workspace(self, workspace_id: str) -> dict | None:
        return await self._workspace_repo.get_workspace_dict(workspace_id)

    async def list_workspaces(self, partition: str) -> list[dict]:
        return await self._workspace_repo.list_workspaces_dict(partition)

    async def create_workspace(
        self,
        workspace_id: str,
        partition: str,
        user_id: int | None = None,
        display_name: str | None = None,
    ) -> None:
        """Create a workspace.

        The 409-on-exists guard lives in the thin router (byte-identical
        non-bracketed body); this is the plain repo create.
        """
        await self._workspace_repo.create_workspace_legacy(
            workspace_id,
            partition,
            user_id,
            display_name,
        )

    async def get_existing_file_ids(self, partition: str, file_ids: list[str]) -> list[str]:
        return list(await self._workspace_repo.get_existing_file_ids(partition, file_ids))

    async def add_files(self, workspace_id: str, file_ids: list[str]) -> list[str]:
        """Associate files; returns any file_ids that were not found."""
        return await self._workspace_repo.add_files_to_workspace(workspace_id, file_ids)

    async def remove_file(self, workspace_id: str, file_id: str) -> bool:
        return await self._workspace_repo.remove_file_from_workspace(workspace_id, file_id)

    async def list_files(self, workspace_id: str) -> list[str]:
        return await self._workspace_repo.list_workspace_files(workspace_id)

    async def get_file_workspaces(self, file_id: str, partition: str) -> list[str]:
        return await self._workspace_repo.get_file_workspaces(file_id, partition)

    # ------------------------------------------------------------------
    # Cross-cutting: delete workspace + clean up orphaned files
    # ------------------------------------------------------------------

    async def delete_workspace(self, partition: str, workspace_id: str) -> dict:
        """Delete the workspace, then fully delete any files it orphaned.

        ``workspace_repo.delete_workspace`` removes the workspace and its
        associations and returns the file_ids that are no longer
        referenced by *any* workspace. Each of those is deleted from the
        vector store and the relational catalog — concurrently, with
        per-file failures collected rather than raised, matching the
        legacy router's ``asyncio.gather(..., return_exceptions=True)``.
        """
        orphaned = await self._workspace_repo.delete_workspace(workspace_id)

        deleted_count = 0
        failed_file_ids: list[str] = []
        if orphaned:
            results = await asyncio.gather(
                *[self._delete_file(file_id, partition) for file_id in orphaned],
                return_exceptions=True,
            )
            for file_id, result in zip(orphaned, results, strict=True):
                if isinstance(result, Exception):
                    logger.warning(
                        "Failed to delete orphaned file from vector store",
                        file_id=file_id,
                        error=str(result),
                    )
                    failed_file_ids.append(file_id)
                else:
                    deleted_count += 1

        return {
            "orphaned_files_deleted": deleted_count,
            "orphaned_files_failed": failed_file_ids,
        }

    async def _delete_file(self, file_id: str, partition: str) -> None:
        """Port of the legacy ``vectordb.delete_file``.

        Drops the file's chunks from the vector store (via the clean
        port: query ids by filter + delete), then detaches it from every
        workspace and removes the relational file row.
        """
        ids = await self._vector_store.query_ids_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
        )
        if ids:
            await self._vector_store.delete(ids, self._collection)
        await self._workspace_repo.remove_file_from_all_workspaces(file_id, partition)
        await self._document_repo.remove_file_from_partition(file_id=file_id, partition=partition)
        logger.info("Deleted orphaned file", file_id=file_id, partition=partition)


__all__ = ["WorkspaceService"]
