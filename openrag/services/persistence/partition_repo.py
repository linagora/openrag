"""Postgres implementation of :class:`PartitionRepository`.

Manages the ``partitions`` table — the global registry of document
collections. The legacy
:class:`components.indexer.vectordb.utils.PartitionFileManager` exposed
``create_partition``, ``delete_partition``, ``list_partitions``,
``partition_exists``, ``get_partition_file_count``, ``get_total_file_count``
here; all six map onto this class.

Deleting a partition explicitly removes ``files`` before the partition row;
memberships and workspaces use FK cascades. Per-uploader ``file_count`` is
decremented in application code (no SQL trigger) so the books stay balanced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from core.ports.partition_repo import PartitionRepository
from core.utils.exceptions import ValidationError
from core.utils.logging import get_logger
from services.persistence.file_count import decrement_file_counts

if TYPE_CHECKING:
    import asyncpg
else:  # pragma: no cover - import shape only matters at runtime
    import asyncpg

# Partition columns that reference a pipeline_presets row, mapped to the
# preset_type they point at. Assigning either column must be guarded against a
# concurrent preset delete (see update_partition).
_PRESET_COLUMN_TYPES = {
    "indexation_preset": "indexation",
    "retrieval_preset": "retrieval",
}
# Partition columns that reference a model_endpoints row, mapped to the
# model_type they point at. Only `chat_llm` is assignment-validated today
# (PartitionService._validate_chat_llm_ref checks the in-memory catalog);
# `embedder` carries no such check, so it is deliberately not listed here.
# Assigning chat_llm must be guarded against a concurrent rename the same way
# a preset assignment is guarded against a concurrent preset delete — see
# update_partition and PgModelEndpointRepository.rename.
_MODEL_ENDPOINT_COLUMN_TYPES = {
    "chat_llm": "llm",
}
_PARTITION_UPDATE_COLUMNS = frozenset(
    {
        "description",
        "embedder",
        "indexation_preset",
        "retrieval_preset",
        "dimension",
        "collection_name",
        "chat_history_depth",
        "chat_llm",
        "generation_prompt_names",
    }
)
_PARTITION_OPERATION_LOCK_NAMESPACE = 20260720

logger = get_logger()


def _partition_updates(fields: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in fields.items() if key in _PARTITION_UPDATE_COLUMNS}


def _endpoint_refs(updates: dict[str, object]) -> dict[str, str]:
    """Model-endpoint-referencing columns in *updates*, mapped to their model_type.

    A ``None`` value clears the (nullable) column rather than pointing it at a
    name, so it needs no existence check.
    """
    return {
        col: model_type
        for col, model_type in _MODEL_ENDPOINT_COLUMN_TYPES.items()
        if col in updates and updates[col] is not None
    }


class _PartitionOperationGuard:
    def __init__(self, repo: PgPartitionRepository, conn: asyncpg.Connection) -> None:
        self._repo = repo
        self._conn = conn

    async def partition_exists(self, name: str) -> bool:
        return await self._repo._partition_exists_on_conn(self._conn, name)

    async def create_partition(self, name: str, user_id: int | None = None, *, max_owned: int | None = None) -> dict:
        return await self._repo._create_partition_on_conn(
            self._conn,
            name,
            user_id=user_id,
            max_owned=max_owned,
        )

    async def delete_partition(self, name: str) -> bool:
        return await self._repo._delete_partition_on_conn(self._conn, name)

    async def update_partition(self, name: str, **fields: object) -> dict | None:
        return await self._repo._update_partition_on_conn(self._conn, name, **fields)

    async def list_partition_rows(self) -> list[dict]:
        return await self._repo._list_partition_rows_on_conn(self._conn)


class PgPartitionRepository(PartitionRepository):
    """asyncpg-backed implementation of :class:`PartitionRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    @asynccontextmanager
    async def partition_operation_lock(self, name: str) -> AsyncIterator[_PartitionOperationGuard]:
        """Hold a cross-process fence for partition deletes and upload admission."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_advisory_lock($1::integer, hashtext($2)::integer)",
                _PARTITION_OPERATION_LOCK_NAMESPACE,
                name,
            )
            try:
                yield _PartitionOperationGuard(self, conn)
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock($1::integer, hashtext($2)::integer)",
                    _PARTITION_OPERATION_LOCK_NAMESPACE,
                    name,
                )

    # ── PartitionRepository port methods ─────────────────────────────

    async def create_partition(self, name: str, user_id: int | None = None, *, max_owned: int | None = None) -> dict:
        """Insert a partition row and grant the creator owner membership.

        Existing partitions are not treated as successful creates. The service
        layer needs that distinction so it does not update preset/config fields
        for a partition it did not create.
        """
        async with self.pool.acquire() as conn:
            return await self._create_partition_on_conn(conn, name, user_id=user_id, max_owned=max_owned)

    async def _create_partition_on_conn(
        self,
        conn: asyncpg.Connection,
        name: str,
        user_id: int | None = None,
        *,
        max_owned: int | None = None,
    ) -> dict:
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

        Counter updates are derived from the rows actually deleted and remain
        clamped at zero so retries and concurrent deletion paths stay safe.
        """
        async with self.pool.acquire() as conn:
            return await self._delete_partition_on_conn(conn, name)

    async def _delete_partition_on_conn(self, conn: asyncpg.Connection, name: str) -> bool:
        async with conn.transaction():
            partition = await conn.fetchrow(
                "SELECT partition FROM partitions WHERE partition = $1 FOR UPDATE",
                name,
            )
            if partition is None:
                logger.bind(
                    operation="delete_partition",
                    deleted_rows=0,
                    counters_adjusted=0,
                ).debug("Catalog partition deletion accounted")
                return False
            deleted_rows = await conn.fetch(
                "DELETE FROM files WHERE partition_name = $1 RETURNING created_by",
                name,
            )
            counters_adjusted = await decrement_file_counts(conn, deleted_rows)
            await conn.execute(
                "DELETE FROM partitions WHERE partition = $1",
                name,
            )
            logger.bind(
                operation="delete_partition",
                deleted_rows=len(deleted_rows),
                counters_adjusted=counters_adjusted,
            ).debug("Catalog partition deletion accounted")
            return True

    async def partition_exists(self, name: str) -> bool:
        return await self._partition_exists_on_conn(
            self.pool,
            name,
        )

    async def _partition_exists_on_conn(self, conn: asyncpg.Connection | asyncpg.Pool, name: str) -> bool:
        return await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM partitions WHERE partition = $1)",
            name,
        )

    # ── Phase 14 — full config row methods ───────────────────────────

    async def get_partition_row(self, name: str) -> dict | None:
        row = await self._get_partition_row_on_conn(
            self.pool,
            name,
        )
        return row

    async def _get_partition_row_on_conn(self, conn: asyncpg.Connection | asyncpg.Pool, name: str) -> dict | None:
        row = await conn.fetchrow(
            "SELECT * FROM partitions WHERE partition = $1",
            name,
        )
        return self._row_to_full_dict(row) if row else None

    async def list_partition_rows(self) -> list[dict]:
        return await self._list_partition_rows_on_conn(self.pool)

    async def _list_partition_rows_on_conn(self, conn: asyncpg.Connection | asyncpg.Pool) -> list[dict]:
        rows = await conn.fetch(
            "SELECT * FROM partitions ORDER BY created_at",
        )
        return [self._row_to_full_dict(r) for r in rows]

    async def update_partition(self, name: str, **fields: object) -> dict | None:
        """Update a partition's config columns.

        When the update assigns a preset column (``indexation_preset`` /
        ``retrieval_preset``) or ``chat_llm``, the write and a DB-authoritative
        existence check run in one transaction that touches ``partitions``
        before ``pipeline_presets`` / ``model_endpoints`` — the same lock order
        :meth:`PgPresetRepository.delete` and :meth:`PgModelEndpointRepository.
        rename` use (both ``LOCK`` ``partitions`` ``IN SHARE MODE`` first). That
        makes an assign and a concurrent delete/rename of the same reference
        serialize without deadlocking, so a partition can never end up pointing
        at a preset or model endpoint that no longer exists under that name:

        * if this UPDATE commits first, the delete/rename's own guard against
          ``partitions`` (a ``COUNT`` for presets, the ``SHARE`` lock itself for
          renames) sees the reference and blocks or refuses accordingly;
        * if the delete/rename commits first, this UPDATE blocks on its
          ``SHARE``-conflicting write, then the follow-up ``SELECT`` sees the
          vanished name and the transaction rolls the write back (raising
          ``PRESET_NOT_FOUND`` / ``MODEL_ENDPOINT_NOT_FOUND``) — instead of
          silently writing back a name a concurrent rename already moved on
          from, which is what a validate-in-memory-then-blind-UPDATE sequence
          could otherwise do.

        ``embedder`` carries no such check — it has no assignment-time
        validation at all today (see ``_MODEL_ENDPOINT_COLUMN_TYPES``), so
        there is nothing here for a concurrent rename to race against.
        """
        updates = _partition_updates(fields)
        if updates:
            preset_refs = {col: _PRESET_COLUMN_TYPES[col] for col in updates if col in _PRESET_COLUMN_TYPES}
            endpoint_refs = _endpoint_refs(updates)
            if preset_refs or endpoint_refs:
                async with self.pool.acquire() as conn:
                    return await self._update_partition_on_conn(conn, name, **fields)
        return await self._update_partition_on_conn(self.pool, name, **fields)

    async def _update_partition_on_conn(
        self,
        conn: asyncpg.Connection | asyncpg.Pool,
        name: str,
        **fields: object,
    ) -> dict | None:
        updates = _partition_updates(fields)
        if not updates:
            return await self._get_partition_row_on_conn(conn, name)

        params: list = [name]
        sets: list[str] = []
        for col, val in updates.items():
            idx = len(params) + 1
            sets.append(f"{col} = ${idx}::jsonb" if col == "generation_prompt_names" else f"{col} = ${idx}")
            params.append(val)
        sql = f"UPDATE partitions SET {', '.join(sets)}, updated_at = now() WHERE partition = $1 RETURNING *"

        preset_refs = {col: _PRESET_COLUMN_TYPES[col] for col in updates if col in _PRESET_COLUMN_TYPES}
        endpoint_refs = _endpoint_refs(updates)
        if not preset_refs and not endpoint_refs:
            row = await conn.fetchrow(sql, *params)
            return self._row_to_full_dict(row) if row else None

        transaction = getattr(conn, "transaction", None)
        if transaction is None:
            raise TypeError("preset/model-endpoint reference updates require a connection transaction")
        async with transaction():
            row = await conn.fetchrow(sql, *params)
            if row is None:
                return None
            for col, preset_type in preset_refs.items():
                exists = await conn.fetchval(
                    "SELECT 1 FROM pipeline_presets WHERE name = $1 AND preset_type = $2",
                    updates[col],
                    preset_type,
                )
                if not exists:
                    raise ValidationError(
                        f"{preset_type.capitalize()} preset '{updates[col]}' does not exist.",
                        code="PRESET_NOT_FOUND",
                    )
            for col, model_type in endpoint_refs.items():
                exists = await conn.fetchval(
                    "SELECT 1 FROM model_endpoints WHERE name = $1 AND model_type = $2",
                    updates[col],
                    model_type,
                )
                if not exists:
                    raise ValidationError(
                        f"{model_type.upper()} endpoint '{updates[col]}' referenced by {col} not found.",
                        code="MODEL_ENDPOINT_NOT_FOUND",
                    )
            return self._row_to_full_dict(row)

    # ── Legacy method names used by the Phase 7C shim ────────────────

    async def get_partition_file_count(self, partition: str) -> int:
        """Number of indexed files in one partition (powers ``document_count``)."""
        return await self.pool.fetchval(
            "SELECT COUNT(*)::int FROM files WHERE partition_name = $1",
            partition,
        )

    async def count_files_by_partition(self) -> dict[str, int]:
        """Return a ``{partition_name: file_count}`` map for all partitions in one query."""
        rows = await self.pool.fetch(
            "SELECT partition_name, COUNT(*)::int AS n FROM files GROUP BY partition_name",
        )
        return {r["partition_name"]: r["n"] for r in rows}

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
            "generation_prompt_names": row["generation_prompt_names"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


__all__ = ["PgPartitionRepository"]
