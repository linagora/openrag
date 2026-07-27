"""PartitionService — partition CRUD, membership, file/chunk reads (Phase 8B.1).

Business logic extracted from ``routers/partition.py`` and the partition
slice of the legacy Ray ``vectordb`` shim. The service talks to the
Phase 7 repositories and the :class:`VectorStore` port directly; it does
not depend on Ray or pymilvus.

``delete_partition`` is the one cross-cutting method — it must drop the
partition's vectors from the store *and* the relational rows. Vector cleanup
runs first through :class:`VectorStore.delete_by_filter`, so a failed vector
delete does not leave catalog rows removed while chunks remain queryable.

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

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import numpy as np
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.indexing.validators import validate_partition_name
from core.models.preset import PartitionConfig
from core.utils.conts import is_internal_metadata_key
from core.utils.exceptions import (
    ConfigError,
    NotFoundError,
    PartitionNotFoundError,
    UserNotFoundError,
    ValidationError,
)
from core.utils.logging import get_logger
from services.workers.task_cancellation import cancel_active_indexing_tasks

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.ports.document_repo import DocumentRepository
    from core.ports.partition_membership_repo import PartitionMembershipRepository
    from core.ports.partition_repo import PartitionRepository
    from core.ports.user_repo import UserRepository
    from core.vector_stores import VectorStore

logger = get_logger()

# Names reserved as cross-partition sentinels (e.g. ``openrag-all`` /
# ``?partitions=all``). A real partition named ``all`` collides with the sentinel
# and the admin partition-list route would expand it to *every* partition — see
# ``list_existant_partitions``. Matched case-insensitively.
_RESERVED_PARTITION_NAMES = frozenset({"all"})
_USER_PAGE_SIZE = 100

# Columns where an explicit ``None`` in a PATCH is a real value (SQL NULL =
# "reset to default"), not the omitted-field sentinel that the None-filter
# in ``update_partition`` gives every other column.
_NULLABLE_COLUMNS = frozenset({"chat_llm"})
_ACTIVE_PARTITION_OPERATIONS: ContextVar[dict[str, Any] | None] = ContextVar(
    "_ACTIVE_PARTITION_OPERATIONS",
    default=None,
)


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
        task_state_manager: Any = None,
        task_state_manager_factory: Callable[[], Any] | None = None,
        task_cancel_timeout: float = 60.0,
    ) -> None:
        self._partition_repo = partition_repo
        self._membership_repo = membership_repo
        self._document_repo = document_repo
        self._vector_store = vector_store
        self._user_repo = user_repo
        self._collection = collection
        self._config = config
        self._task_state_manager = task_state_manager
        self._task_state_manager_factory = task_state_manager_factory
        self._task_cancel_timeout = task_cancel_timeout
        self._partition_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Existence guards (mirror the legacy _check_* helpers, core exceptions)
    # ------------------------------------------------------------------

    async def _ensure_partition(self, partition: str) -> None:
        operation = self._active_partition_operation(partition)
        if not await self._partition_exists_for_operation(partition, operation=operation):
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
            operation = self._active_partition_operation(partition)
            return await self._partition_exists_for_operation(partition, operation=operation)
        except Exception as e:  # pragma: no cover - defensive, matches legacy
            logger.exception("Partition existence check failed.", partition=partition, error=str(e))
            return False

    @asynccontextmanager
    async def indexing_admission(self, partition: str) -> AsyncIterator[bool]:
        """Serialize upload admission with partition deletion.

        Yields whether the partition row existed while the admission fence was
        held. Uploads that observed an existing partition must not auto-create it
        later if a concurrent delete removes the row before the worker writes the
        catalog record.
        """
        async with self._partition_operation_lock(partition) as operation:
            active_operations = dict(_ACTIVE_PARTITION_OPERATIONS.get() or {})
            active_operations[partition] = operation
            token = _ACTIVE_PARTITION_OPERATIONS.set(active_operations)
            try:
                yield await self._partition_exists_for_operation(partition, operation=operation)
            finally:
                _ACTIVE_PARTITION_OPERATIONS.reset(token)

    @asynccontextmanager
    async def _partition_operation_lock(self, partition: str) -> AsyncIterator[Any]:
        lock_factory = getattr(self._partition_repo, "partition_operation_lock", None)
        if lock_factory is not None:
            async with lock_factory(partition) as operation:
                yield operation
            return

        lock = self._partition_locks.setdefault(partition, asyncio.Lock())
        async with lock:
            yield None

    async def _partition_exists_for_operation(self, partition: str, *, operation: Any = None) -> bool:
        exists = getattr(operation, "partition_exists", None)
        if exists is not None:
            return bool(await exists(partition))
        return await self._partition_repo.partition_exists(name=partition)

    async def _create_partition_for_operation(
        self,
        partition: str,
        *,
        user_id: int | None,
        max_owned: int | None,
        operation: Any = None,
    ) -> dict:
        create_partition = getattr(operation, "create_partition", None)
        if create_partition is not None:
            return await create_partition(partition, user_id=user_id, max_owned=max_owned)
        return await self._partition_repo.create_partition(name=partition, user_id=user_id, max_owned=max_owned)

    async def _delete_partition_for_operation(self, partition: str, *, operation: Any = None) -> bool:
        delete_partition = getattr(operation, "delete_partition", None)
        if delete_partition is not None:
            return bool(await delete_partition(partition))
        return await self._partition_repo.delete_partition(name=partition)

    async def _update_partition_for_operation(
        self,
        partition: str,
        *,
        operation: Any = None,
        **fields: object,
    ) -> dict | None:
        update_partition = getattr(operation, "update_partition", None)
        if update_partition is not None:
            return await update_partition(partition, **fields)
        return await self._partition_repo.update_partition(partition, **fields)

    async def _list_partition_rows_for_operation(self, *, operation: Any = None) -> list[dict]:
        list_partition_rows = getattr(operation, "list_partition_rows", None)
        if list_partition_rows is not None:
            return await list_partition_rows()
        return await self._partition_repo.list_partition_rows()

    def _active_partition_operation(self, partition: str | None = None) -> Any:
        active_operations = _ACTIVE_PARTITION_OPERATIONS.get() or {}
        if partition is not None:
            return active_operations.get(partition)
        return next((operation for operation in active_operations.values() if operation is not None), None)

    async def _ensure_partition_for_operation(self, partition: str, *, operation: Any = None) -> None:
        if not await self._partition_exists_for_operation(partition, operation=operation):
            logger.warning(f"Partition '{partition}' does not exist.")
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")

    async def list_partitions(self) -> list[dict]:
        return await self._partition_repo.list_partitions()

    async def file_counts_by_partition(self) -> dict[str, int]:
        """Return a ``{partition: document_count}`` map for all partitions (one query)."""
        return await self._partition_repo.count_files_by_partition()

    async def list_partition_summaries(self) -> dict[str, dict]:
        """Per-partition stored config columns + ``document_count``, keyed by name.

        Lightweight list view: returns the stored columns (description, embedder,
        preset references, dimension, chat config) WITHOUT resolving the
        indexation/retrieval pipelines — so this stays two queries regardless of
        partition count. Pipeline resolution is reserved for the single-partition
        detail (``get_partition_config``). Values are JSON-ready.
        """
        rows = await self._partition_repo.list_partition_rows()
        counts = await self.file_counts_by_partition()
        summaries: dict[str, dict] = {}
        for r in rows:
            name = r["partition"]
            created = r.get("created_at")
            summaries[name] = {
                "partition": name,
                "description": r.get("description") or "",
                "embedder": r.get("embedder") or "default",
                "indexation_preset": r.get("indexation_preset") or "default",
                "retrieval_preset": r.get("retrieval_preset") or "default",
                "dimension": r.get("dimension"),
                "chat_history_depth": r.get("chat_history_depth") or self._legacy_chat_history_depth_fallback(),
                "chat_llm": r.get("chat_llm"),
                "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
                "document_count": counts.get(name, 0),
            }
        return summaries

    async def create_partition(
        self,
        partition: str,
        user_id: int,
        *,
        max_owned: int | None = None,
        description: str = "",
        embedder: str = "default",
        indexation_preset: str = "default",
        retrieval_preset: str = "default",
        chat_history_depth: int = 4,
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
        # Reserved-name check first so a name that normalises to a reserved
        # sentinel (e.g. "  all  ") returns the specific RESERVED_PARTITION_NAME
        # error rather than the generic identifier-allowlist rejection.
        if partition.strip().lower() in _RESERVED_PARTITION_NAMES:
            raise ValidationError(
                f"Partition name '{partition}' is reserved.",
                status_code=400,
                code="RESERVED_PARTITION_NAME",
            )
        validate_partition_name(partition)
        operation = self._active_partition_operation(partition)
        if await self._partition_exists_for_operation(partition, operation=operation):
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
            if chat_llm:
                self._validate_chat_llm_ref(chat_llm)

        await self._create_partition_for_operation(
            partition,
            user_id=user_id,
            max_owned=max_owned,
            operation=operation,
        )

        # Persist the config columns (the insert only sets server defaults)
        # and re-resolve the in-memory cache. Only done in the Phase 14 flow
        # where a config was supplied.
        if self._config is not None:
            await self._update_partition_for_operation(partition, operation=operation, **config_fields)
            await self.load_partitions()

        logger.info(f"Partition '{partition}' created by user_id {user_id}.")

    async def delete_partition(self, partition: str) -> None:
        """Drop a partition's vectors *and* relational rows (cross-cutting)."""
        async with self._partition_operation_lock(partition) as operation:
            await self._delete_partition_locked(partition, operation=operation)

    async def _delete_partition_locked(self, partition: str, *, operation: Any = None) -> None:
        await self._ensure_partition_for_operation(partition, operation=operation)
        task_state_manager = self._task_state_manager
        if task_state_manager is None and self._task_state_manager_factory is not None:
            task_state_manager = self._task_state_manager_factory()
        if task_state_manager is not None:
            cancelled = await cancel_active_indexing_tasks(
                task_state_manager,
                partition=partition,
                timeout=self._task_cancel_timeout,
            )
            if cancelled:
                logger.info("Cancelled active indexing tasks before deleting partition", partition=partition)

        # The shared Milvus collection is created lazily on the first insert
        # system-wide, so on a fresh stack (nothing ever indexed) it doesn't
        # exist; deleting by filter would raise (e.g. DescribeCollectionException)
        # even though there are no chunks to clean up.
        # No collection means no chunks to clean up — skip the vector cleanup.
        collection_exists = await self._vector_store.collection_exists(self._collection)
        if collection_exists:
            deleted = await self._vector_store.delete_by_filter({"partition": partition})
            logger.info(
                "Deleted points from partition",
                partition=partition,
                count=deleted,
            )
        else:
            logger.info(
                "No vector collection to clean up before deleting partition",
                partition=partition,
            )
        deleted = await self._delete_partition_for_operation(partition, operation=operation)
        if deleted and self._config is not None:
            self._config.partitions.pop(partition, None)
        if collection_exists:
            try:
                deleted = await self._vector_store.delete_by_filter({"partition": partition})
            except Exception as exc:
                logger.warning(
                    "Post-delete vector cleanup failed after partition row removal",
                    partition=partition,
                    error=str(exc),
                )
                raise
            else:
                logger.info(
                    "Deleted race-leftover points from partition",
                    partition=partition,
                    count=deleted,
                )
        logger.info("Partition successfully deleted.", partition=partition)

    async def update_partition(self, partition: str, **fields: object) -> dict | None:
        """Update a partition's config columns and re-resolve the cache.

        ``None`` values are ignored (so partial PATCH semantics work) —
        except for the ``_NULLABLE_COLUMNS`` (``chat_llm``), where an
        explicit ``None`` clears the column back to its default. When a
        preset reference changes, the merged row is validated against the
        in-memory cache *before* the write so an unknown preset name fails
        fast, and an assigned ``chat_llm`` must name a catalogued LLM
        endpoint; the repository additionally re-checks preset existence
        atomically under the write's transaction, closing the race with a
        concurrent preset delete (see
        :meth:`PgPartitionRepository.update_partition`).
        """
        await self._ensure_partition(partition)
        updates = {k: v for k, v in fields.items() if v is not None or k in _NULLABLE_COLUMNS}

        if self._config is not None and updates:
            current = await self._partition_repo.get_partition_row(partition)
            if current is None:
                raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
            self._validate_preset_refs({**current, **updates})
            # Only the incoming value is checked — a *stored* name that went
            # stale (endpoint deleted later) must not block unrelated PATCHes;
            # QueryService falls back to the default LLM for those at runtime.
            if updates.get("chat_llm"):
                self._validate_chat_llm_ref(updates["chat_llm"])

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
        detail = self._partition_detail(row, self.resolve_partition_row(row))
        detail["document_count"] = await self._partition_repo.get_partition_file_count(partition)
        return detail

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

    def _validate_chat_llm_ref(self, chat_llm: str) -> None:
        """Assignment-time check: ``chat_llm`` must name a catalogued LLM endpoint.

        Only guards assignment — a stored name can go stale afterwards (the
        endpoint may be renamed or deleted), which QueryService tolerates by
        falling back to the default LLM at request time.
        """
        if chat_llm not in self._require_config().models.llm:
            raise ValidationError(
                f"LLM endpoint '{chat_llm}' referenced by chat_llm not found.",
                code="MODEL_ENDPOINT_NOT_FOUND",
            )

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
            "chat_history_depth": row.get("chat_history_depth") or self._legacy_chat_history_depth_fallback(),
            "chat_llm": row.get("chat_llm"),
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
            # This value feeds Settings.partitions, so it's what
            # QueryService._resolve_chat_history_depth actually reads at chat time.
            chat_history_depth=row.get("chat_history_depth") or self._legacy_chat_history_depth_fallback(),
            chat_llm=row.get("chat_llm"),
        )

    async def load_partitions(self) -> None:
        """Resolve every partition row and swap the in-memory cache atomically.

        Uses clear()+update() so any reference already held to the
        ``config.partitions`` dict stays valid after the swap.
        """
        cfg = self._require_config()
        rows = await self._list_partition_rows_for_operation(operation=self._active_partition_operation())
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

    #: Depth used when neither a partition row nor the global config yields a usable value.
    _CHAT_HISTORY_DEPTH_DEFAULT = 4

    def _legacy_chat_history_depth_fallback(self) -> int:
        """Value substituted for a partition row still holding the pre-guard ``0``.

        ``0`` used to mean "inherit the global default" (``config.rag.chat_history_depth``,
        see ``QueryService._resolve_chat_history_depth``). New writes can no longer
        produce ``0`` (``ge=1`` on ``CreatePartitionRequest``/``UpdatePartitionRequest``),
        but a row written before that guard existed may still have it stored — read it
        from the live config, not a hardcoded constant, so a legacy row keeps resolving
        to the *current* global default exactly as it did before, even if an operator
        changes ``rag.chat_history_depth`` later.

        ``RAGConfig.chat_history_depth`` itself carries no lower bound, so a deployment
        may configure it to ``0`` (or negative). Since this value feeds
        ``PartitionConfig(chat_history_depth: ge=1)``, returning ``< 1`` here would make
        ``load_partitions()`` raise ``ValidationError`` at startup. Clamp such values to
        the hardcoded default so a legacy row can never crash partition loading.
        """
        if self._config is None:
            return self._CHAT_HISTORY_DEPTH_DEFAULT
        configured = self._config.rag.chat_history_depth
        return configured if configured >= 1 else self._CHAT_HISTORY_DEPTH_DEFAULT

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
        return [{k: v for k, v in row.items() if k != "text" and not is_internal_metadata_key(k)} for row in rows]

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
                if is_internal_metadata_key(k):
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

    async def list_member_candidates(self, partition: str) -> list[dict]:
        """Return non-member user identities suitable for a membership picker."""
        await self._ensure_partition(partition)
        members = await self._membership_repo.list_partition_members(partition)
        member_ids = {member["user_id"] for member in members}

        candidates: list[dict] = []
        offset = 0
        while True:
            users = await self._user_repo.list_users(offset=offset, limit=_USER_PAGE_SIZE)
            candidates.extend(
                {
                    "user_id": user.id,
                    "display_name": user.display_name,
                }
                for user in users
                if user.id not in member_ids
            )
            if len(users) < _USER_PAGE_SIZE:
                break
            offset += _USER_PAGE_SIZE

        return candidates

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
