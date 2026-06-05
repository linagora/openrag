"""PartitionService — partition CRUD, membership, file/chunk reads (Phase 8B.1).

Business logic extracted from ``routers/partition.py`` and the partition
slice of the legacy Ray ``vectordb`` shim. The service talks to the
Phase 7 repositories and the :class:`VectorStore` port directly; it does
not depend on Ray or pymilvus.

``delete_partition`` is the one cross-cutting method — it must drop the
partition's vectors from the store *and* the relational rows. It is
performed through the clean :class:`VectorStore` port
(``query_ids_by_filter`` + ``delete``) rather than a Milvus-specific
filter delete, so PartitionService stays backend-agnostic.

Chunk reads return plain dicts (never LangChain ``Document`` objects —
8H forbids LangChain in orchestrators); the thin router builds the
``request.url_for`` links and final response shape.

Constructor notes (two args beyond the plan's four, both to preserve
legacy behaviour without widening into Ray/config): ``collection`` (the
vector-store collection name the legacy shim read from
``config.vectordb.collection_name``) and ``user_repo`` (needed to
reproduce the ``VDBUserNotFound`` 404 the legacy ``add_partition_member``
raised). The container supplies both from settings/the catalog store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
from core.utils.exceptions import (
    NotFoundError,
    PartitionNotFoundError,
    UserNotFoundError,
    ValidationError,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.ports.document_repo import DocumentRepository
    from core.ports.partition_membership_repo import PartitionMembershipRepository
    from core.ports.partition_repo import PartitionRepository
    from core.ports.user_repo import UserRepository
    from core.vector_stores import VectorStore

logger = get_logger()


class PartitionService:
    """Partition lifecycle, membership and read-through orchestration."""

    def __init__(
        self,
        *,
        partition_repo: PartitionRepository,
        membership_repo: PartitionMembershipRepository,
        document_repo: DocumentRepository,
        vector_store: VectorStore,
        user_repo: UserRepository,
        collection: str,
    ) -> None:
        self._partition_repo = partition_repo
        self._membership_repo = membership_repo
        self._document_repo = document_repo
        self._vector_store = vector_store
        self._user_repo = user_repo
        self._collection = collection

    # ------------------------------------------------------------------
    # Existence guards (mirror the legacy _check_* helpers, core exceptions)
    # ------------------------------------------------------------------

    async def _ensure_partition(self, partition: str) -> None:
        if not await self._partition_repo.partition_exists(name=partition):
            logger.warning(f"Partition '{partition}' does not exist.")
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")

    async def _ensure_user_exists(self, user_id: int) -> None:
        if not await self._user_repo.user_exists(user_id):
            logger.warning(f"User with ID {user_id} does not exist.")
            raise UserNotFoundError(f"User with ID {user_id} does not exist.")

    async def _ensure_membership(self, partition: str, user_id: int) -> None:
        await self._ensure_partition(partition)
        await self._ensure_user_exists(user_id)
        if not await self._membership_repo.user_is_partition_member(user_id, partition):
            raise NotFoundError(
                f"User with ID {user_id} is not a member of partition '{partition}'.",
                code="MEMBERSHIP_NOT_FOUND",
            )

    async def file_exists(self, file_id: str, partition: str) -> bool:
        try:
            return await self._document_repo.file_exists_in_partition(
                file_id=file_id,
                partition=partition,
            )
        except Exception as e:  # pragma: no cover - defensive, matches legacy
            logger.exception("File existence check failed.", file_id=file_id, partition=partition, error=str(e))
            return False

    # ------------------------------------------------------------------
    # Partition CRUD
    # ------------------------------------------------------------------

    async def partition_exists(self, partition: str) -> bool:
        try:
            return await self._partition_repo.partition_exists(name=partition)
        except Exception as e:  # pragma: no cover - defensive, matches legacy
            logger.exception("Partition existence check failed.", partition=partition, error=str(e))
            return False

    async def list_partitions(self) -> list[dict]:
        return await self._partition_repo.list_partitions()

    async def create_partition(self, partition: str, user_id: int) -> None:
        """Create a partition owned by ``user_id``.

        The 409-on-exists check lives in the thin router (it returns a
        non-bracketed ``{"detail": ...}`` body that must stay identical);
        this raises only if the race is lost between that check and here.
        """
        if await self._partition_repo.partition_exists(name=partition):
            raise ValidationError(
                f"Partition '{partition}' already exists.",
                status_code=409,
                code="PARTITION_EXISTS",
            )
        await self._partition_repo.create_partition(name=partition, user_id=user_id)
        logger.info(f"Partition '{partition}' created by user_id {user_id}.")

    async def delete_partition(self, partition: str) -> None:
        """Drop a partition's vectors *and* relational rows (cross-cutting)."""
        await self._ensure_partition(partition)
        await self._partition_repo.delete_partition(name=partition)
        ids = await self._vector_store.query_ids_by_filter(
            self._collection,
            {"partition": partition},
        )
        if ids:
            try:
                deleted = await self._vector_store.delete(ids, self._collection)
                logger.info("Deleted points from partition", partition=partition, count=deleted)
            except Exception as e:
                logger.error(
                    "Failed to delete chunks from vector store after removing partition from database; "
                    "reconciliation task required",
                    partition=partition,
                    chunk_count=len(ids),
                    error=str(e),
                )
        logger.info("Partition successfully deleted.", partition=partition)

    # ------------------------------------------------------------------
    # File / chunk reads
    # ------------------------------------------------------------------

    async def list_files(self, partition: str, limit: int | None = None) -> list[dict]:
        await self._ensure_partition(partition)
        result = await self._document_repo.list_partition_files(partition=partition, limit=limit)
        return result.get("files", [])

    async def get_file_chunks(self, partition: str, file_id: str, limit: int = 2000) -> list[dict]:
        """Return chunk rows (``_id`` kept, ``text`` dropped) for one file.

        The router builds the extract links and strips ``_id`` from the
        surfaced metadata, exactly as before.
        """
        if not await self.file_exists(file_id, partition):
            raise NotFoundError(
                f"'{file_id}' not found in partition '{partition}'",
                code="FILE_NOT_FOUND",
            )
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
            output_fields=["*"],
        )
        if len(rows) > limit:
            rows = rows[:limit]
        return [{k: v for k, v in row.items() if k != "text"} for row in rows]

    async def list_all_chunks(self, partition: str, include_embedding: bool = True) -> list[dict]:
        """Return ``{"content", "metadata"}`` dicts for every chunk."""
        await self._ensure_partition(partition)
        excluded = {"text"} if include_embedding else {"text", "vector"}
        output_fields = ["*", "vector"] if include_embedding else ["*"]
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition},
            output_fields=output_fields,
        )

        def _meta(row: dict[str, Any]) -> dict[str, Any]:
            meta: dict[str, Any] = {}
            for k, v in row.items():
                if k in excluded:
                    continue
                if k == "vector":
                    # Legacy surfaced the embedding as a flat string.
                    v = str(np.array(v).flatten().tolist())
                meta[k] = v
            return meta

        return [{"content": row.get("text"), "metadata": _meta(row)} for row in rows]

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    async def list_members(self, partition: str) -> list[dict]:
        await self._ensure_partition(partition)
        return await self._membership_repo.list_partition_members(partition)

    async def add_member(self, partition: str, user_id: int, role: str) -> None:
        await self._ensure_partition(partition)
        await self._ensure_user_exists(user_id)
        await self._membership_repo.add_partition_member(partition, user_id, role)
        logger.info(f"User_id {user_id} added to partition '{partition}'.")

    async def remove_member(self, partition: str, user_id: int) -> None:
        await self._ensure_membership(partition, user_id)
        await self._membership_repo.remove_partition_member(partition, user_id)
        logger.info(f"User_id {user_id} removed from partition '{partition}'.")

    async def update_role(self, partition: str, user_id: int, new_role: str) -> None:
        await self._ensure_membership(partition, user_id)
        await self._membership_repo.update_partition_member_role(partition, user_id, new_role)
        logger.info(f"User_id {user_id} role updated to '{new_role}' in partition '{partition}'.")

    # ------------------------------------------------------------------
    # Document relationships
    # ------------------------------------------------------------------

    async def get_related_files(self, partition: str, relationship_id: str) -> list[dict]:
        return await self._document_repo.get_files_by_relationship(
            partition=partition,
            relationship_id=relationship_id,
        )

    async def get_file_ancestors(
        self,
        partition: str,
        file_id: str,
        max_ancestor_depth: int | None = None,
    ) -> list[dict]:
        if not await self.file_exists(file_id, partition):
            raise NotFoundError(
                f"'{file_id}' not found in partition '{partition}'",
                code="FILE_NOT_FOUND",
            )
        return await self._document_repo.get_file_ancestors(
            partition=partition,
            file_id=file_id,
            max_ancestor_depth=max_ancestor_depth,
        )


__all__ = ["PartitionService"]
