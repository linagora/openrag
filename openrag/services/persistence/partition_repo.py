"""Postgres implementation of :class:`PartitionRepository`.

Manages the ``partitions`` table — the global registry of document
collections. The legacy
:class:`components.indexer.vectordb.utils.PartitionFileManager` exposed
``create_partition``, ``delete_partition``, ``list_partitions``,
``partition_exists``, ``get_partition_file_count``, ``get_total_file_count``
here; all six map onto this class.

Deleting a partition cascades to ``files``, ``partition_memberships``,
and ``workspaces`` via the FK ``ON DELETE CASCADE`` rules in the schema.
Per-uploader ``file_count`` is decremented in application code (no SQL
trigger) so the books stay balanced.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from core.ports.partition_repo import PartitionRepository
from core.utils.exceptions import ValidationError

if TYPE_CHECKING:
    import asyncpg
else:  # pragma: no cover - import shape only matters at runtime
    import asyncpg


class PgPartitionRepository(PartitionRepository):
    """asyncpg-backed implementation of :class:`PartitionRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ── PartitionRepository port methods ─────────────────────────────

    async def create_partition(self, name: str, user_id: int | None = None, *, max_owned: int | None = None) -> dict:
        """Insert a partition row and grant the creator owner membership.

        Existing partitions are not treated as successful creates. The service
        layer needs that distinction so it does not update preset/config fields
        for a partition it did not create.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if user_id is not None and max_owned is not None and max_owned >= 0:
                    await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", user_id)
                row = await conn.fetchrow(
                    "SELECT * FROM partitions WHERE partition = $1",
                    name,
                )
                if row is not None:
                    raise ValidationError(
                        f"Partition '{name}' already exists.",
                        status_code=409,
                        code="PARTITION_EXISTS",
                    )
                if user_id is not None and max_owned is not None and max_owned >= 0:
                    owned = await conn.fetchval(
                        """
                        SELECT COUNT(*)::int FROM partition_memberships
                        WHERE user_id = $1 AND role = 'owner'
                        """,
                        user_id,
                    )
                    if owned >= max_owned:
                        raise ValidationError(
                            f"Partition limit reached ({max_owned}). Contact an administrator.",
                            status_code=403,
                            code="PARTITION_LIMIT_EXCEEDED",
                        )
                try:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO partitions (partition, created_at)
                        VALUES ($1, NOW())
                        RETURNING *
                        """,
                        name,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise ValidationError(
                        f"Partition '{name}' already exists.",
                        status_code=409,
                        code="PARTITION_EXISTS",
                    ) from exc
                if user_id is not None:
                    await conn.execute(
                        """
                        INSERT INTO partition_memberships
                            (partition_name, user_id, role, added_at)
                        VALUES ($1, $2, 'owner', NOW())
                        ON CONFLICT (partition_name, user_id) DO NOTHING
                        """,
                        name,
                        user_id,
                    )
        return self._row_to_dict(row)

    async def get_partition(self, name: str) -> dict | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM partitions WHERE partition = $1",
            name,
        )
        return self._row_to_dict(row) if row else None

    async def list_partitions(self) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM partitions ORDER BY created_at")
        return [self._row_to_dict(r) for r in rows]

    async def delete_partition(self, name: str) -> bool:
        """Delete a partition + its files, memberships, and workspaces.

        ``files.partition_name`` has no ``ON DELETE CASCADE`` (the legacy
        ORM relied on SQLAlchemy's Python-side cascade), so we delete file
        rows explicitly before the partition. ``workspace_files.file_id``
        cascades, so workspace links clean up with the files.
        ``partition_memberships`` and ``workspaces`` cascade from the
        partition row.

        Mirrors the legacy bookkeeping: before deleting we count files per
        uploader and decrement each uploader's ``file_count`` by that
        amount (clamped at zero) so quotas stay accurate.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM partitions WHERE partition = $1",
                    name,
                )
                if not exists:
                    return False
                uploader_counts = await conn.fetch(
                    """
                    SELECT created_by, COUNT(*)::int AS n
                    FROM files
                    WHERE partition_name = $1 AND created_by IS NOT NULL
                    GROUP BY created_by
                    """,
                    name,
                )
                await conn.execute(
                    "DELETE FROM files WHERE partition_name = $1",
                    name,
                )
                await conn.execute(
                    "DELETE FROM partitions WHERE partition = $1",
                    name,
                )
                for r in uploader_counts:
                    await conn.execute(
                        "UPDATE users SET file_count = GREATEST(file_count - $1, 0) WHERE id = $2",
                        r["n"],
                        r["created_by"],
                    )
                return True

    async def partition_exists(self, name: str) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM partitions WHERE partition = $1)",
            name,
        )

    # ── Phase 14 — full config row methods ───────────────────────────

    async def get_partition_row(self, name: str) -> dict | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM partitions WHERE partition = $1",
            name,
        )
        return self._row_to_full_dict(row) if row else None

    async def list_partition_rows(self) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM partitions ORDER BY created_at",
        )
        return [self._row_to_full_dict(r) for r in rows]

    async def update_partition(self, name: str, **fields: object) -> dict | None:
        _ALLOWED = frozenset(
            {
                "description",
                "embedder",
                "indexation_preset",
                "retrieval_preset",
                "dimension",
                "collection_name",
                "chat_history_depth",
                "chat_llm",
            }
        )
        updates = {k: v for k, v in fields.items() if k in _ALLOWED}
        if not updates:
            return await self.get_partition_row(name)

        params: list = [name]
        sets: list[str] = []
        for col, val in updates.items():
            idx = len(params) + 1
            sets.append(f"{col} = ${idx}")
            params.append(val)

        row = await self.pool.fetchrow(
            f"UPDATE partitions SET {', '.join(sets)}, updated_at = now() WHERE partition = $1 RETURNING *",
            *params,
        )
        return self._row_to_full_dict(row) if row else None

    # ── Legacy method names used by the Phase 7C shim ────────────────

    async def get_partition_file_count(self, partition: str) -> int:
        """TODO(phase-9): remove."""
        return await self.pool.fetchval(
            "SELECT COUNT(*)::int FROM files WHERE partition_name = $1",
            partition,
        )

    async def get_total_file_count(self) -> int:
        """TODO(phase-9): remove."""
        return await self.pool.fetchval("SELECT COUNT(*)::int FROM files")

    # ── Row → dict helper ────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: asyncpg.Record) -> dict:
        """Shape mirrors the legacy ``Partition.to_dict()`` ORM helper."""
        created = row["created_at"]
        return {
            "partition": row["partition"],
            "created_at": created.isoformat() if created else None,
        }

    @staticmethod
    def _row_to_full_dict(row: asyncpg.Record) -> dict:
        """Full partition row including all Phase 14 config columns."""
        return {
            "partition": row["partition"],
            "description": row["description"],
            "embedder": row["embedder"],
            "indexation_preset": row["indexation_preset"],
            "retrieval_preset": row["retrieval_preset"],
            "dimension": row["dimension"],
            "collection_name": row["collection_name"],
            "chat_history_depth": row["chat_history_depth"],
            "chat_llm": row["chat_llm"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


__all__ = ["PgPartitionRepository"]
