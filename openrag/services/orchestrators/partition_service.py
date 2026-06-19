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
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig
from core.utils.exceptions import (
    ConfigError,
    NotFoundError,
    PartitionNotFoundError,
    UserNotFoundError,
    ValidationError,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.ports.document_repo import DocumentRepository
    from core.ports.partition_membership_repo import PartitionMembershipRepository
    from core.ports.partition_repo import PartitionRepository
    from core.ports.user_repo import UserRepository
    from core.vector_stores import VectorStore

logger = get_logger()


def _validate_limit(limit: int | None) -> None:
    """Reject negative ``limit`` values before they reach ``rows[:limit]``.

    A negative bound would silently drop tail rows (e.g. ``-1`` returns all but
    the last chunk) instead of capping the result, so treat it as a 422.
    """
    if limit is not None and limit < 0:
        raise ValidationError("`limit` must be greater than or equal to 0.", code="INVALID_LIMIT")


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
        config: Settings | None = None,
    ) -> None:
        self._partition_repo = partition_repo
        self._membership_repo = membership_repo
        self._document_repo = document_repo
        self._vector_store = vector_store
        self._user_repo = user_repo
        self._collection = collection
        self._config = config

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

    async def create_partition(
        self,
        partition: str,
        user_id: int,
        *,
        description: str = "",
        embedder: str = "default",
        indexation_preset: str = "default",
        retrieval_preset: str = "default",
        chat_history_depth: int = 0,
        chat_llm: str | None = None,
    ) -> None:
        """Create a partition owned by ``user_id`` with preset references.

        The 409-on-exists check lives in the thin router (it returns a
        non-bracketed ``{"detail": ...}`` body that must stay identical);
        this raises only if the race is lost between that check and here.

        When ``config`` was supplied to the service, the referenced presets
        are validated *before* the row is written (so a bad preset name fails
        fast and atomically), the non-default config columns are persisted,
        and the in-memory partition cache is re-resolved.
        """
        if await self._partition_repo.partition_exists(name=partition):
            raise ValidationError(
                f"Partition '{partition}' already exists.",
                status_code=409,
                code="PARTITION_EXISTS",
            )

        config_fields = {
            "description": description,
            "embedder": embedder,
            "indexation_preset": indexation_preset,
            "retrieval_preset": retrieval_preset,
            "chat_history_depth": chat_history_depth,
            "chat_llm": chat_llm,
        }

        # Validate the preset references before touching the DB.
        if self._config is not None:
            self._validate_preset_refs({"partition": partition, **config_fields})

        await self._partition_repo.create_partition(name=partition, user_id=user_id)

        # Persist the config columns (the insert only sets server defaults)
        # and re-resolve the in-memory cache. Only done in the Phase 14 flow
        # where a config was supplied.
        if self._config is not None:
            await self._partition_repo.update_partition(partition, **config_fields)
            await self.load_partitions()

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

    async def update_partition(self, partition: str, **fields: object) -> dict | None:
        """Update a partition's config columns and re-resolve the cache.

        ``None`` values are ignored (so partial PATCH semantics work). When a
        preset reference changes, the merged row is validated *before* the
        write so an unknown preset name fails fast.
        """
        await self._ensure_partition(partition)
        updates = {k: v for k, v in fields.items() if v is not None}

        if self._config is not None and updates:
            current = await self._partition_repo.get_partition_row(partition)
            if current is None:
                raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
            self._validate_preset_refs({**current, **updates})

        result = await self._partition_repo.update_partition(partition, **updates)

        if self._config is not None:
            await self.load_partitions()

        logger.info("Partition updated.", partition=partition, fields=sorted(updates))
        return result

    async def get_partition_config(self, partition: str) -> dict:
        """Return the resolved Phase 14 detail for a partition.

        Shapes a ``PartitionDetailResponse``: the stored preset references plus
        the fully resolved indexation/retrieval pipelines. Raises 404 if the
        partition does not exist.
        """
        self._require_config()
        row = await self._partition_repo.get_partition_row(partition)
        if row is None:
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
        return self._partition_detail(row, self.resolve_partition_row(row))

    async def update_partition_config(self, partition: str, **fields: object) -> dict:
        """Update a partition's preset references and return the resolved detail."""
        await self.update_partition(partition, **fields)
        return await self.get_partition_config(partition)

    def _validate_preset_refs(self, row: dict) -> None:
        """Validate a row's preset references for create/update.

        The preset names come from user input, so a missing preset is a client
        error: translate the resolver's ConfigError (which maps to 500) into a
        422 ValidationError.
        """
        try:
            self.resolve_partition_row(row)
        except ConfigError as exc:
            raise ValidationError(exc.message, code="PRESET_NOT_FOUND") from exc

    def _partition_detail(self, row: dict, cfg: PartitionConfig) -> dict:
        """Shape a resolved row into the ``PartitionDetailResponse`` payload."""
        return {
            "name": cfg.name,
            "description": cfg.description,
            "embedder": cfg.embedder,
            "indexation_preset": row.get("indexation_preset") or "default",
            "retrieval_preset": row.get("retrieval_preset") or "default",
            "indexation_pipeline": cfg.indexation.model_dump(mode="json"),
            "retrieval_pipeline": cfg.retrieval.model_dump(mode="json"),
            "dimension": row.get("dimension"),
            "created_at": row.get("created_at"),
        }

    # ------------------------------------------------------------------
    # Preset resolution + in-memory cache
    # ------------------------------------------------------------------

    def resolve_partition_row(self, row: dict) -> PartitionConfig:
        """Resolve a partition DB row into a fully-validated PartitionConfig.

        Looks the referenced preset names up in ``config.presets`` and builds
        the Pydantic pipeline configs. Raises :class:`ConfigError` if either
        referenced preset is missing — the caller decides whether that is a
        startup failure or a 4xx on create/update.
        """
        cfg = self._require_config()
        name = row["partition"]
        idx_name = row.get("indexation_preset") or "default"
        ret_name = row.get("retrieval_preset") or "default"

        idx_preset = cfg.presets.indexation.get(idx_name)
        if idx_preset is None:
            raise ConfigError(f"Indexation preset '{idx_name}' referenced by partition '{name}' not found.")
        ret_preset = cfg.presets.retrieval.get(ret_name)
        if ret_preset is None:
            raise ConfigError(f"Retrieval preset '{ret_name}' referenced by partition '{name}' not found.")

        return PartitionConfig(
            name=name,
            description=row.get("description") or "",
            embedder=row.get("embedder") or "default",
            indexation=IndexationPipelineConfig(**idx_preset),
            retrieval=RetrievalPipelineConfig(**ret_preset),
            collection_name=row.get("collection_name"),
            chat_history_depth=row.get("chat_history_depth") or 0,
            chat_llm=row.get("chat_llm"),
        )

    async def load_partitions(self) -> None:
        """Resolve every partition row and swap the in-memory cache atomically.

        Uses clear()+update() so any reference already held to the
        ``config.partitions`` dict stays valid after the swap.
        """
        cfg = self._require_config()
        rows = await self._partition_repo.list_partition_rows()
        resolved = {row["partition"]: self.resolve_partition_row(row) for row in rows}

        cache = cfg.partitions
        cache.clear()
        cache.update(resolved)
        logger.info("Loaded partition configs.", n_partitions=len(resolved))

    async def seed_default_partition(self, user_id: int = 1) -> None:
        """Ensure the 'default' partition exists with default presets."""
        if await self._partition_repo.partition_exists(name="default"):
            return
        await self._partition_repo.create_partition(name="default", user_id=user_id)
        logger.info("Seeded 'default' partition.")

    def _require_config(self) -> Settings:
        if self._config is None:
            raise ConfigError("PartitionService was constructed without a config; preset resolution unavailable.")
        return self._config

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
        _validate_limit(limit)
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

    async def list_all_chunks(
        self,
        partition: str,
        include_embedding: bool = True,
        file_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Return ``{"content", "metadata"}`` dicts for chunks in a partition.

        ``file_id`` scopes the query to a single file, pushing the filter down
        to the vector store so the document detail view costs O(file) instead of
        O(partition). ``limit`` caps the number of chunks returned (a defensive
        bound for pathologically large files).
        """
        _validate_limit(limit)
        await self._ensure_partition(partition)
        excluded = {"text"} if include_embedding else {"text", "vector"}
        output_fields = ["*", "vector"] if include_embedding else ["*"]
        filters: dict[str, Any] = {"partition": partition}
        if file_id is not None:
            filters["file_id"] = file_id
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            filters,
            output_fields=output_fields,
        )
        if limit is not None and len(rows) > limit:
            rows = rows[:limit]

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
