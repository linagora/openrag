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

from core.models.workspace import WorkspaceScope
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.ports.document_repo import DocumentRepository
    from core.ports.workspace_repo import WorkspaceRepository
    from core.vector_stores import VectorStore

logger = get_logger()

# TEMPORARY, until workspace ids are unique per partition (linagora/openrag#1019).
# The workspaces table keys workspaces by workspace_id alone, so two partitions
# cannot use the same id: cozy-stack gives the Drive root of every instance the
# workspace "io-cozy-files-root-dir", and only the first instance got it (the
# others got a 409). New workspaces are stored under "<partition>/<workspace_id>":
# "/" is allowed in neither a partition name nor a workspace id, so such a key
# never collides with a bare one. The API still takes and returns bare ids, and
# the workspaces created before this patch keep their bare key.
WORKSPACE_KEY_SEPARATOR = "/"


def workspace_key(partition: str, workspace_id: str) -> str:
    """The key a new workspace of ``partition`` is stored under."""
    return f"{partition}{WORKSPACE_KEY_SEPARATOR}{workspace_id}"


def public_workspace_id(key: str) -> str:
    """The workspace id the API exposes for a stored key."""
    return key.split(WORKSPACE_KEY_SEPARATOR, 1)[-1]


def _public_dict(ws: dict) -> dict:
    return {**ws, "workspace_id": public_workspace_id(ws["workspace_id"])}


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

    async def get_workspace(self, key: str) -> dict | None:
        """The workspace stored under ``key``, with its public id."""
        ws = await self._workspace_repo.get_workspace_dict(key)
        return _public_dict(ws) if ws else None

    async def find_workspace_key(self, partition: str, workspace_id: str) -> str | None:
        """The key of the workspace ``workspace_id`` of ``partition``, or None.

        Tries the partition-scoped key first, then the bare key of a
        workspace created before the scoping, which must belong to
        ``partition``: a bare id held by another partition is not ours.
        """
        for key in (workspace_key(partition, workspace_id), workspace_id):
            ws = await self._workspace_repo.get_workspace_dict(key)
            if ws and ws["partition_name"] == partition:
                return key
        return None

    async def list_workspaces(self, partition: str) -> list[dict]:
        return [_public_dict(ws) for ws in await self._workspace_repo.list_workspaces_dict(partition)]

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
            workspace_key(partition, workspace_id),
            partition,
            user_id,
            display_name,
        )

    async def get_existing_file_ids(self, partition: str, file_ids: list[str]) -> list[str]:
        return list(await self._workspace_repo.get_existing_file_ids(partition, file_ids))

    async def get_existing_file_ids_any_partition(self, file_ids: list[str]) -> list[str]:
        return list(await self._workspace_repo.get_existing_file_ids_any_partition(file_ids))

    async def add_files(self, key: str, file_ids: list[str]) -> list[str]:
        """Associate files; returns any file_ids that were not found."""
        return await self._workspace_repo.add_files_to_workspace(key, file_ids)

    async def remove_file(self, key: str, file_id: str) -> bool:
        return await self._workspace_repo.remove_file_from_workspace(key, file_id)

    async def list_files(self, key: str) -> list[str]:
        return await self._workspace_repo.list_workspace_files(key)

    async def get_file_workspaces(self, file_id: str, partition: str) -> list[str]:
        keys = await self._workspace_repo.get_file_workspaces(file_id, partition)
        return [public_workspace_id(key) for key in keys]

    # ------------------------------------------------------------------
    # Search-scope resolution (single source of truth for workspace-scoped
    # search / chat — issue #706)
    # ------------------------------------------------------------------

    async def resolve_scope(self, workspace_id: str, allowed_partitions: list[str]) -> WorkspaceScope | None:
        """Resolve ``workspace_id`` to its owning partition and file allowlist.

        Returns ``None`` when the workspace does not exist *or* exists in a
        partition outside ``allowed_partitions`` — the two cases are
        intentionally indistinguishable to the caller so a workspace living
        in another tenant's partition is never revealed to exist. ``"all"``
        in ``allowed_partitions`` (the ``openrag-all`` / multi-partition
        sentinel) accepts a workspace from any partition, matching how
        partition access is resolved elsewhere.

        The returned ``file_ids`` may be empty — a workspace with no files
        yet is valid and must scope the search to zero results, not fall
        back to the full partition.

        With ``"all"``, only a workspace created before the partition
        scoping of the keys is found: the partition of a scoped key cannot
        be guessed (temporary, see ``workspace_key``).
        """
        for partition in allowed_partitions:
            if partition == "all":
                continue
            key = await self.find_workspace_key(partition, workspace_id)
            if key:
                return await self._scope(key, workspace_id, partition)
        if "all" in allowed_partitions:
            ws = await self._workspace_repo.get_workspace_dict(workspace_id)
            if ws:
                return await self._scope(workspace_id, workspace_id, ws["partition_name"])
        return None

    async def _scope(self, key: str, workspace_id: str, partition: str) -> WorkspaceScope:
        file_ids = await self._workspace_repo.list_workspace_files(key)
        return WorkspaceScope(workspace_id=workspace_id, partition=partition, file_ids=file_ids)

    # ------------------------------------------------------------------
    # Cross-cutting: delete workspace + clean up orphaned files
    # ------------------------------------------------------------------

    async def delete_workspace(self, partition: str, key: str) -> dict:
        """Delete the workspace, then fully delete any files it orphaned.

        ``workspace_repo.delete_workspace`` removes the workspace and its
        associations and returns the file_ids that are no longer
        referenced by *any* workspace. Each of those is deleted from the
        vector store and the relational catalog — concurrently, with
        per-file failures collected rather than raised, matching the
        legacy router's ``asyncio.gather(..., return_exceptions=True)``.
        """
        orphaned = await self._workspace_repo.delete_workspace(key)

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


__all__ = ["WorkspaceService", "public_workspace_id", "workspace_key"]
