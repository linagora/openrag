"""Postgres implementation of :class:`WorkspaceRepository`.

Backs the ``workspaces`` table and the ``workspace_files`` many-to-many
join. The legacy
:class:`components.indexer.vectordb.utils.PartitionFileManager` exposed
ten workspace methods that all map onto this class:
``create_workspace``, ``list_workspaces``, ``get_workspace``,
``delete_workspace``, ``add_files_to_workspace``,
``remove_file_from_workspace``, ``list_workspace_files``,
``get_file_workspaces``, ``get_existing_file_ids``,
``remove_file_from_all_workspaces``.

The join references the canonical ``files.id`` integer PK (not the
opaque ``file_id`` string), so deletion cascades correctly — when a
``files`` row goes away the workspace_files entries it backed go with
it without any application-side bookkeeping. Conversely the workspace
APIs accept and emit the human ``file_id`` form; the repo translates at
the boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from core.models.workspace import Workspace
from core.ports.workspace_repo import WorkspaceRepository

if TYPE_CHECKING:
    import asyncpg


class PgWorkspaceRepository(WorkspaceRepository):
    """asyncpg-backed implementation of :class:`WorkspaceRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ── WorkspaceRepository port methods ─────────────────────────────

    async def create_workspace(self, workspace: Workspace) -> Workspace:
        row = await self.pool.fetchrow(
            """
            INSERT INTO workspaces (workspace_id, partition_name,
                                    created_by, display_name, created_at)
            VALUES ($1, $2, $3, $4, COALESCE($5, NOW()))
            RETURNING *
            """,
            workspace.workspace_id,
            workspace.partition,
            workspace.created_by,
            workspace.display_name,
            workspace.created_at,
        )
        return self._row_to_workspace(row)

    async def get_workspace(self, workspace_id: str) -> Workspace | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM workspaces WHERE workspace_id = $1",
            workspace_id,
        )
        return self._row_to_workspace(row) if row else None

    async def list_workspaces(self, partition: str) -> list[Workspace]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM workspaces
            WHERE partition_name = $1
            ORDER BY created_at
            """,
            partition,
        )
        return [self._row_to_workspace(r) for r in rows]

    async def delete_workspace(self, workspace_id: str) -> list[str]:
        """Delete the workspace and return the orphaned ``file_id`` list.

        An orphan = a file currently in this workspace and in no other.
        Because ``workspace_files.file_id`` is an integer FK to
        ``files.id``, every workspace_files row already has a backing
        files row; the orphan check therefore reduces to
        "file_id NOT IN (other workspaces' file_ids)".

        Returning the orphans (rather than auto-deleting them) keeps the
        deletion of the underlying file optional — the legacy router
        loops over the list and calls the indexer's file-delete path so
        the Milvus side is cleaned up too.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                orphan_rows = await conn.fetch(
                    """
                    SELECT f.file_id
                    FROM workspace_files wf
                    JOIN files f ON f.id = wf.file_id
                    WHERE wf.workspace_id = $1
                      AND wf.file_id NOT IN (
                          SELECT file_id FROM workspace_files
                          WHERE workspace_id <> $1
                      )
                    """,
                    workspace_id,
                )
                await conn.execute(
                    "DELETE FROM workspaces WHERE workspace_id = $1",
                    workspace_id,
                )
        return [r["file_id"] for r in orphan_rows]

    async def add_files_to_workspace(
        self,
        workspace_id: str,
        file_ids: list[str],
    ) -> list[str]:
        """Attach files identified by their string ``file_id`` to a workspace.

        Returns the list of supplied ``file_ids`` that do not exist in
        the workspace's partition — callers surface these to the user as
        "not found".
        """
        if not file_ids:
            return []
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                workspace = await conn.fetchrow(
                    "SELECT partition_name FROM workspaces WHERE workspace_id = $1",
                    workspace_id,
                )
                if workspace is None:
                    return list(file_ids)
                partition = workspace["partition_name"]
                resolved = await conn.fetch(
                    """
                    SELECT file_id, id FROM files
                    WHERE file_id = ANY($1::text[]) AND partition_name = $2
                    """,
                    file_ids,
                    partition,
                )
                id_map = {r["file_id"]: r["id"] for r in resolved}
                missing = [fid for fid in file_ids if fid not in id_map]
                if id_map:
                    # Insert each row separately with ON CONFLICT DO NOTHING.
                    # asyncpg has no native bulk-with-conflict; the row count
                    # is bounded by file_ids so the loop is fine here.
                    for file_pk in id_map.values():
                        await conn.execute(
                            """
                            INSERT INTO workspace_files (workspace_id, file_id)
                            VALUES ($1, $2)
                            ON CONFLICT ON CONSTRAINT uix_workspace_file DO NOTHING
                            """,
                            workspace_id,
                            file_pk,
                        )
        return missing

    async def remove_file_from_workspace(
        self,
        workspace_id: str,
        file_id: str,
    ) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                workspace = await conn.fetchrow(
                    "SELECT partition_name FROM workspaces WHERE workspace_id = $1",
                    workspace_id,
                )
                if workspace is None:
                    return False
                file_pk = await conn.fetchval(
                    """
                    SELECT id FROM files
                    WHERE file_id = $1 AND partition_name = $2
                    """,
                    file_id,
                    workspace["partition_name"],
                )
                if file_pk is None:
                    return False
                result = await conn.execute(
                    """
                    DELETE FROM workspace_files
                    WHERE workspace_id = $1 AND file_id = $2
                    """,
                    workspace_id,
                    file_pk,
                )
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError):
            return False

    async def list_workspace_files(self, workspace_id: str) -> list[str]:
        rows = await self.pool.fetch(
            """
            SELECT f.file_id
            FROM workspace_files wf
            JOIN files f ON f.id = wf.file_id
            WHERE wf.workspace_id = $1
            """,
            workspace_id,
        )
        return [r["file_id"] for r in rows]

    async def get_file_workspaces(
        self,
        file_id: str,
        partition: str,
    ) -> list[str]:
        """Workspaces containing ``file_id``, scoped to ``partition``.

        Scoping is necessary because a given ``file_id`` string is unique
        only within a partition — the underlying ``files`` rows are
        distinct PKs across partitions.
        """
        rows = await self.pool.fetch(
            """
            SELECT wf.workspace_id
            FROM workspace_files wf
            JOIN files f ON f.id = wf.file_id
            JOIN workspaces w ON w.workspace_id = wf.workspace_id
            WHERE f.file_id = $1
              AND f.partition_name = $2
              AND w.partition_name = $2
            """,
            file_id,
            partition,
        )
        return [r["workspace_id"] for r in rows]

    async def get_existing_file_ids(
        self,
        partition: str,
        file_ids: list[str],
    ) -> set[str]:
        if not file_ids:
            return set()
        rows = await self.pool.fetch(
            """
            SELECT file_id FROM files
            WHERE file_id = ANY($1::text[]) AND partition_name = $2
            """,
            file_ids,
            partition,
        )
        return {r["file_id"] for r in rows}

    async def remove_file_from_all_workspaces(
        self,
        file_id: str,
        partition: str,
    ) -> None:
        """Detach a file from every workspace in its partition.

        Called from the file-delete path so workspace integrity is
        restored before the underlying file row goes away. A no-op when
        the file does not exist in the partition.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                file_pk = await conn.fetchval(
                    """
                    SELECT id FROM files
                    WHERE file_id = $1 AND partition_name = $2
                    """,
                    file_id,
                    partition,
                )
                if file_pk is None:
                    return
                await conn.execute(
                    """
                    DELETE FROM workspace_files
                    WHERE file_id = $1
                      AND workspace_id IN (
                          SELECT workspace_id FROM workspaces
                          WHERE partition_name = $2
                      )
                    """,
                    file_pk,
                    partition,
                )

    # ── Legacy method names used by the Phase 7C shim ────────────────

    async def create_workspace_legacy(
        self,
        workspace_id: str,
        partition: str,
        user_id: int | None,
        display_name: str | None = None,
    ) -> None:
        """TODO(phase-9): remove. Positional-arg mirror of legacy ``create_workspace``."""
        await self.pool.execute(
            """
            INSERT INTO workspaces (workspace_id, partition_name,
                                    created_by, display_name, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            """,
            workspace_id,
            partition,
            user_id,
            display_name,
        )

    async def list_workspaces_dict(self, partition: str) -> list[dict]:
        """TODO(phase-9): remove. Legacy router-facing dict shape."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM workspaces
            WHERE partition_name = $1
            ORDER BY created_at
            """,
            partition,
        )
        return [self._row_to_dict(r) for r in rows]

    async def get_workspace_dict(self, workspace_id: str) -> dict | None:
        """TODO(phase-9): remove. Legacy router-facing dict shape."""
        row = await self.pool.fetchrow(
            "SELECT * FROM workspaces WHERE workspace_id = $1",
            workspace_id,
        )
        return self._row_to_dict(row) if row else None

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_workspace(row: asyncpg.Record) -> Workspace:
        return Workspace(
            workspace_id=row["workspace_id"],
            partition=row["partition_name"],
            display_name=row["display_name"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_dict(row: asyncpg.Record) -> dict:
        return {
            "workspace_id": row["workspace_id"],
            "partition_name": row["partition_name"],
            "display_name": row["display_name"],
            "created_by": row["created_by"],
            "created_at": str(row["created_at"]) if row["created_at"] else None,
        }


__all__ = ["PgWorkspaceRepository"]
