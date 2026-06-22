"""Postgres implementation of :class:`DocumentRepository`.

Backed by the ``files`` table — the canonical record of every file indexed
into OpenRAG. The legacy :class:`components.indexer.vectordb.utils.PartitionFileManager`
exposed eight methods here that are decomposed onto this class:
``add_file_to_partition``, ``remove_file_from_partition``,
``update_file_metadata_in_db``, ``update_file_in_partition``,
``list_partition_files``, ``file_exists_in_partition``,
``get_files_by_relationship``, ``get_file_ancestors``.

The new port methods (``create_document`` / ``get_document`` / ...) take the
clean :class:`DocumentRecord` domain model — those are what Phase 8
orchestrators will call. The legacy method names are kept on the concrete
class (not on the ABC) so the Phase 7C shim can delegate to them unchanged.
Both sets read and write the same rows.

Status / error_message / filename / created_at fields exist on
``DocumentRecord`` but not on the ``files`` table — they are derived from
``file_metadata`` or set to safe defaults. Tracking these in their own
columns is a post-refactoring feature.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.models.catalog import DocumentRecord, DocumentStatus
from core.ports.document_repo import DocumentRepository

if TYPE_CHECKING:
    import asyncpg

# Note on JSON: ``ConnectionManager.initialize`` registers a json/jsonb codec
# on every connection, so reading a JSON column yields a Python dict and
# binding a dict to a JSON parameter is encoded transparently. The repo
# therefore never calls ``json.dumps`` itself.


class PgDocumentRepository(DocumentRepository):
    """asyncpg-backed implementation of :class:`DocumentRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ── DocumentRepository port methods ──────────────────────────────

    async def create_document(self, doc: DocumentRecord) -> DocumentRecord:
        """Insert a document row keyed by (file_id, partition).

        The port-level ``DocumentRecord.id`` is treated as the natural
        ``file_id`` — the legacy schema uses an integer surrogate PK but
        every caller identifies documents by ``file_id``. If ``doc.id`` is
        a default UUID and ``doc.file_id`` is also set, the explicit
        ``file_id`` wins.
        """
        file_id = doc.file_id or doc.id
        metadata = dict(doc.metadata or {})
        if doc.filename and "filename" not in metadata:
            metadata["filename"] = doc.filename
        if doc.status and doc.status != DocumentStatus.QUEUED:
            metadata["status"] = doc.status.value
        if doc.error_message:
            metadata["error_message"] = doc.error_message
        await self.pool.execute(
            """
            INSERT INTO files (file_id, partition_name, file_metadata,
                               indexation_config, created_by, relationship_id, parent_id)
            VALUES ($1, $2, $3::json, $4::jsonb, $5, $6, $7)
            """,
            file_id,
            doc.partition,
            metadata,
            doc.indexation_config,
            doc.created_by,
            doc.relationship_id,
            doc.parent_id,
        )
        return doc.model_copy(update={"file_id": file_id, "metadata": metadata})

    async def get_document(self, document_id: str) -> DocumentRecord | None:
        """Fetch a document by ``file_id`` (any partition).

        The current schema does not enforce ``file_id`` uniqueness across
        partitions, so this returns the first match. Callers that need
        partition-scoped lookup should use
        :meth:`file_exists_in_partition` or the legacy
        :meth:`list_partition_files`.
        """
        row = await self.pool.fetchrow(
            "SELECT * FROM files WHERE file_id = $1 LIMIT 1",
            document_id,
        )
        return self._row_to_document(row) if row else None

    async def list_documents(
        self,
        partition: str | list[str] | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[DocumentRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if isinstance(partition, str):
            params.append(partition)
            clauses.append(f"partition_name = ${len(params)}")
        elif isinstance(partition, list) and partition:
            params.append(partition)
            clauses.append(f"partition_name = ANY(${len(params)}::text[])")
        if status:
            # status is stored inside file_metadata; we filter on the JSON path
            params.append(status)
            clauses.append(f"file_metadata->>'status' = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = await self.pool.fetch(
            f"SELECT * FROM files {where} ORDER BY id DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
        return [self._row_to_document(r) for r in rows]

    async def update_document(self, document_id: str, **fields: Any) -> DocumentRecord | None:
        """Patch a document row by ``file_id``.

        Accepts the port's domain field names — ``metadata``,
        ``status``, ``error_message``, ``relationship_id``, ``parent_id``,
        ``filename``. ``status`` / ``error_message`` / ``filename`` are
        folded into ``file_metadata`` since the schema has no dedicated
        columns for them.
        """
        if not fields:
            return await self.get_document(document_id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM files WHERE file_id = $1 LIMIT 1",
                document_id,
            )
            if row is None:
                return None
            metadata = dict(row["file_metadata"] or {})
            sets: list[str] = []
            params: list[Any] = []

            for json_only in ("filename", "status", "error_message"):
                if json_only in fields:
                    value = fields.pop(json_only)
                    if json_only == "status" and hasattr(value, "value"):
                        value = value.value
                    metadata[json_only] = value
            if "metadata" in fields:
                merged = fields.pop("metadata") or {}
                metadata.update(merged)
            # We always rewrite file_metadata so JSON-only updates are persisted.
            params.append(metadata)
            sets.append(f"file_metadata = ${len(params)}::json")
            if "indexation_config" in fields:
                params.append(fields.pop("indexation_config"))
                sets.append(f"indexation_config = ${len(params)}::jsonb")

            for column in ("relationship_id", "parent_id", "created_by"):
                if column in fields:
                    params.append(fields.pop(column))
                    sets.append(f"{column} = ${len(params)}")
            if "partition" in fields:
                params.append(fields.pop("partition"))
                sets.append(f"partition_name = ${len(params)}")

            # Silently ignore any unknown keys to match Pydantic-style flexibility.
            params.append(row["id"])
            await conn.execute(
                f"UPDATE files SET {', '.join(sets)} WHERE id = ${len(params)}",
                *params,
            )
            updated = await conn.fetchrow("SELECT * FROM files WHERE id = $1", row["id"])
        return self._row_to_document(updated) if updated else None

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document by ``file_id`` across any partition.

        Decrements the uploader's ``file_count`` (clamped at zero) to keep
        quota accounting honest, matching :meth:`remove_file_from_partition`.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, created_by FROM files WHERE file_id = $1 LIMIT 1",
                    document_id,
                )
                if row is None:
                    return False
                await conn.execute("DELETE FROM files WHERE id = $1", row["id"])
                if row["created_by"] is not None:
                    await conn.execute(
                        "UPDATE users SET file_count = GREATEST(file_count - 1, 0) WHERE id = $1",
                        row["created_by"],
                    )
                return True

    async def delete_documents_by_partition(self, partition: str) -> int:
        """Bulk-delete every file in a partition and decrement uploader counts."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                uploader_rows = await conn.fetch(
                    """
                    SELECT created_by, COUNT(*)::int AS n
                    FROM files
                    WHERE partition_name = $1 AND created_by IS NOT NULL
                    GROUP BY created_by
                    """,
                    partition,
                )
                result = await conn.execute(
                    "DELETE FROM files WHERE partition_name = $1",
                    partition,
                )
                for r in uploader_rows:
                    await conn.execute(
                        "UPDATE users SET file_count = GREATEST(file_count - $1, 0) WHERE id = $2",
                        r["n"],
                        r["created_by"],
                    )
        # asyncpg returns 'DELETE <n>' as the command tag.
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def count_documents(
        self,
        partition: str | list[str] | None = None,
        status: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if isinstance(partition, str):
            params.append(partition)
            clauses.append(f"partition_name = ${len(params)}")
        elif isinstance(partition, list) and partition:
            params.append(partition)
            clauses.append(f"partition_name = ANY(${len(params)}::text[])")
        if status:
            params.append(status)
            clauses.append(f"file_metadata->>'status' = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self.pool.fetchval(f"SELECT COUNT(*)::int FROM files {where}", *params)

    async def file_exists_in_partition(self, file_id: str, partition: str) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM files WHERE file_id = $1 AND partition_name = $2)",
            file_id,
            partition,
        )

    # ── Legacy method names used by the Phase 7C shim ────────────────
    # These are NOT on the ABC. Phase 8 orchestrators must not depend on
    # them — they exist solely so the shim can keep every legacy caller
    # working unchanged until Phase 9 deletes the actor. Mark TODO so they
    # are easy to grep for and remove later.

    async def add_file_to_partition(  # noqa: PLR0913 — legacy signature pinned
        self,
        file_id: str,
        partition: str,
        file_metadata: dict | None = None,
        user_id: int | None = None,
        relationship_id: str | None = None,
        parent_id: str | None = None,
        indexation_config: dict | None = None,
    ) -> bool:
        """TODO(phase-9): remove. Mirror of legacy ``add_file_to_partition``.

        Creates the partition row on first use (legacy behaviour). Returns
        ``False`` if a row with the same (file_id, partition) already exists.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchval(
                    "SELECT 1 FROM files WHERE file_id = $1 AND partition_name = $2",
                    file_id,
                    partition,
                )
                if existing:
                    return False

                # Auto-create partition + first-owner membership when missing —
                # legacy side-effect documented in the phase-7 spec.
                created = await conn.fetchval(
                    """
                    INSERT INTO partitions (partition, created_at)
                    VALUES ($1, NOW())
                    ON CONFLICT (partition) DO NOTHING
                    RETURNING 1
                    """,
                    partition,
                )
                if created and user_id is None:
                    raise ValueError("Cannot auto-create a partition without a user_id")
                if created and user_id is not None:
                    await conn.execute(
                        """
                        INSERT INTO partition_memberships (partition_name, user_id, role, added_at)
                        VALUES ($1, $2, 'owner', NOW())
                        ON CONFLICT (partition_name, user_id) DO NOTHING
                        """,
                        partition,
                        user_id,
                    )

                await conn.execute(
                    """
                    INSERT INTO files (file_id, partition_name, file_metadata,
                                       indexation_config, created_by, relationship_id, parent_id)
                    VALUES ($1, $2, $3::json, $4::jsonb, $5, $6, $7)
                    """,
                    file_id,
                    partition,
                    file_metadata or {},
                    indexation_config,
                    user_id,
                    relationship_id,
                    parent_id,
                )
                if user_id is not None:
                    await conn.execute(
                        "UPDATE users SET file_count = file_count + 1 WHERE id = $1",
                        user_id,
                    )
                return True

    async def remove_file_from_partition(self, file_id: str, partition: str) -> bool:
        """TODO(phase-9): remove. Mirror of legacy ``remove_file_from_partition``."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, created_by FROM files WHERE file_id = $1 AND partition_name = $2",
                    file_id,
                    partition,
                )
                if row is None:
                    return False
                await conn.execute("DELETE FROM files WHERE id = $1", row["id"])
                if row["created_by"] is not None:
                    await conn.execute(
                        "UPDATE users SET file_count = GREATEST(file_count - 1, 0) WHERE id = $1",
                        row["created_by"],
                    )
                return True

    async def update_file_metadata_in_db(
        self,
        file_id: str,
        partition: str,
        file_metadata: dict,
    ) -> bool:
        """TODO(phase-9): remove. Updates ``file_metadata`` + syncs structured columns.

        Mirrors the legacy behaviour: when the new metadata blob contains
        ``relationship_id`` or ``parent_id`` keys, the dedicated columns are
        rewritten too so the JSON never diverges from the structured fields.
        """
        rel_id = file_metadata.get("relationship_id") if "relationship_id" in file_metadata else None
        parent_id = file_metadata.get("parent_id") if "parent_id" in file_metadata else None
        sets = ["file_metadata = $1::json"]
        params: list[Any] = [file_metadata]
        if "relationship_id" in file_metadata:
            params.append(rel_id)
            sets.append(f"relationship_id = ${len(params)}")
        if "parent_id" in file_metadata:
            params.append(parent_id)
            sets.append(f"parent_id = ${len(params)}")
        params.extend([file_id, partition])
        result = await self.pool.execute(
            f"""
            UPDATE files SET {", ".join(sets)}
            WHERE file_id = ${len(params) - 1} AND partition_name = ${len(params)}
            """,
            *params,
        )
        return result.endswith(" 1")

    _UNSET = object()

    async def update_file_in_partition(
        self,
        file_id: str,
        partition: str,
        file_metadata: dict | None = None,
        relationship_id: object = _UNSET,
        parent_id: object = _UNSET,
        indexation_config: object = _UNSET,
    ) -> bool:
        """TODO(phase-9): remove. PUT-style in-place update.

        Preserves the underlying ``files.id`` so workspace FK rows stay
        valid. Pass ``relationship_id=None`` / ``parent_id=None``
        explicitly to clear; omit the kwarg to leave the column alone.
        """
        sets: list[str] = []
        params: list[Any] = []
        if file_metadata is not None:
            params.append(file_metadata)
            sets.append(f"file_metadata = ${len(params)}::json")
        if relationship_id is not self._UNSET:
            params.append(relationship_id)
            sets.append(f"relationship_id = ${len(params)}")
        if parent_id is not self._UNSET:
            params.append(parent_id)
            sets.append(f"parent_id = ${len(params)}")
        if indexation_config is not self._UNSET:
            params.append(indexation_config)
            sets.append(f"indexation_config = ${len(params)}::jsonb")
        if not sets:
            # Match legacy: report whether the row exists at all.
            return await self.file_exists_in_partition(file_id, partition)
        params.extend([file_id, partition])
        result = await self.pool.execute(
            f"""
            UPDATE files SET {", ".join(sets)}
            WHERE file_id = ${len(params) - 1} AND partition_name = ${len(params)}
            """,
            *params,
        )
        return result.endswith(" 1")

    async def list_partition_files(
        self,
        partition: str,
        limit: int | None = None,
    ) -> dict:
        """TODO(phase-9): remove. Returns ``{"files": [...]}`` shape used by routers."""
        sql = "SELECT * FROM files WHERE partition_name = $1"
        params: list[Any] = [partition]
        if limit is not None:
            params.append(limit)
            sql += f" LIMIT ${len(params)}"
        rows = await self.pool.fetch(sql, *params)
        if not rows:
            return {}
        return {"files": [self._row_to_dict(r) for r in rows]}

    async def get_files_by_relationship(
        self,
        partition: str,
        relationship_id: str,
    ) -> list[dict]:
        """TODO(phase-9): remove."""
        rows = await self.pool.fetch(
            "SELECT * FROM files WHERE partition_name = $1 AND relationship_id = $2",
            partition,
            relationship_id,
        )
        return [self._row_to_dict(r) for r in rows]

    async def get_file_ids_by_relationship(
        self,
        partition: str,
        relationship_id: str,
    ) -> list[str]:
        """TODO(phase-9): remove."""
        rows = await self.pool.fetch(
            "SELECT file_id FROM files WHERE partition_name = $1 AND relationship_id = $2",
            partition,
            relationship_id,
        )
        return [r["file_id"] for r in rows]

    async def get_file_ancestors(
        self,
        partition: str,
        file_id: str,
        max_ancestor_depth: int | None = None,
    ) -> list[dict]:
        """TODO(phase-9): remove. Recursive CTE walking ``parent_id`` upward.

        Returns a list ordered from root → self (depth DESC). When
        ``max_ancestor_depth`` is given, the recursion stops once the
        accumulated depth meets the cap. The cap is additionally clamped
        at ``retriever.max_ancestor_depth_cap`` (operator-tunable, default
        1000) so a self-referential or cyclic ``parent_id`` chain can't
        loop indefinitely.
        """
        from core.config import load_config

        hard_cap = int(load_config().retriever.max_ancestor_depth_cap)
        effective_cap = hard_cap
        if max_ancestor_depth is not None:
            effective_cap = min(max_ancestor_depth, hard_cap)
        params: list[Any] = [file_id, partition, effective_cap]
        depth_filter = f"AND a.depth < ${len(params)}"
        rows = await self.pool.fetch(
            f"""
            WITH RECURSIVE ancestors AS (
                SELECT id, file_id, partition_name, parent_id, file_metadata,
                       relationship_id, 0 AS depth
                FROM files
                WHERE file_id = $1 AND partition_name = $2
                  AND relationship_id IS NOT NULL
                UNION ALL
                SELECT f.id, f.file_id, f.partition_name, f.parent_id,
                       f.file_metadata, f.relationship_id, a.depth + 1
                FROM files f
                INNER JOIN ancestors a
                  ON f.file_id = a.parent_id
                 AND f.partition_name = a.partition_name
                 AND f.relationship_id IS NOT NULL
                 {depth_filter}
            )
            SELECT * FROM ancestors ORDER BY depth DESC
            """,
            *params,
        )
        out: list[dict] = []
        for r in rows:
            metadata = r["file_metadata"] or {}
            out.append(
                {
                    "file_id": r["file_id"],
                    "partition": r["partition_name"],
                    "parent_id": r["parent_id"],
                    "relationship_id": r["relationship_id"],
                    "depth": r["depth"],
                    **metadata,
                },
            )
        return out

    async def get_ancestor_file_ids(
        self,
        partition: str,
        file_id: str,
        max_ancestor_depth: int | None = None,
    ) -> list[str]:
        """TODO(phase-9): remove."""
        ancestors = await self.get_file_ancestors(partition, file_id, max_ancestor_depth)
        return [a["file_id"] for a in ancestors]

    # ── Row → domain helpers ─────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: asyncpg.Record) -> dict:
        """Replica of the legacy ``File.to_dict()`` ORM shape.

        The legacy routers consume this exact shape (``partition``,
        ``file_id``, ``relationship_id``, ``parent_id`` plus every metadata
        key flattened in). Used by the shim's pass-through calls.
        """
        metadata = row["file_metadata"] or {}
        indexed_at = row["indexed_at"]
        return {
            "partition": row["partition_name"],
            "file_id": row["file_id"],
            "relationship_id": row["relationship_id"],
            "parent_id": row["parent_id"],
            **metadata,
            # Authoritative system insert time, materialized on the row. Placed
            # after the spread so the column wins over any ``indexed_at`` the
            # copy/restore path copies into file_metadata from chunk metadata
            # (``_file_metadata_from_chunk``). Distinct from the client-supplied
            # ``created_at`` temporal field, which stays in file_metadata.
            "indexed_at": indexed_at.isoformat() if indexed_at else None,
        }

    @staticmethod
    def _row_to_document(row: asyncpg.Record) -> DocumentRecord:
        metadata = dict(row["file_metadata"] or {})
        status_raw = metadata.pop("status", None)
        error_message = metadata.pop("error_message", None)
        filename = metadata.pop("filename", "") or ""
        try:
            status = DocumentStatus(status_raw) if status_raw else DocumentStatus.QUEUED
        except ValueError:
            status = DocumentStatus.QUEUED
        return DocumentRecord(
            id=row["file_id"],
            file_id=row["file_id"],
            filename=filename,
            partition=row["partition_name"],
            metadata=metadata,
            status=status,
            error_message=error_message,
            created_by=row["created_by"],
            relationship_id=row["relationship_id"],
            parent_id=row["parent_id"],
            indexation_config=row["indexation_config"],
        )


__all__ = ["PgDocumentRepository"]
