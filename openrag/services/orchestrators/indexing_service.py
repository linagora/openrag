"""IndexingService — file ingest orchestration.

Business logic extracted from ``routers/indexer.py``: metadata assembly,
existence/workspace checks, and task dispatch. Indexing jobs are routed
through :class:`~core.indexing.dispatcher.IndexingDispatcher` so this
service stays Ray-free.

The thin router keeps HTTP transport only: file save to disk (IO),
``request.url_for`` link building, the shared ``Depends`` auth wrappers,
and the guards whose exact ``{"detail": ...}`` body the legacy endpoints
returned via ``HTTPException``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.utils.conts import strip_protected_metadata
from core.utils.exceptions import AuthError, PartitionNotFoundError, ValidationError
from core.utils.filename import extract_temporal_fields
from core.utils.logging import get_logger
from core.utils.partition_limits import max_partitions_for_user

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.indexing.dispatcher import IndexingDispatcher
    from core.ports.document_repo import DocumentRepository
    from core.ports.workspace_repo import WorkspaceRepository
    from services.orchestrators.partition_service import PartitionService

logger = get_logger()

# Client-supplied datetime fields lifted into queryable metadata.
TEMPORAL_FIELDS = ["created_at"]
_ROLE_HIERARCHY: dict[str, int] = {"viewer": 1, "editor": 2, "owner": 3}


def _human_readable_size(size_bytes: int) -> str:
    """Bytes → human-readable string (e.g. ``'2.40 MB'``).

    Kept private here so the orchestrator does not depend on the HTTP
    layer.
    """
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class IndexingService:
    """File upload/delete/copy/metadata orchestration over the worker layer."""

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        workspace_repo: WorkspaceRepository,
        dispatcher: IndexingDispatcher,
        config: Settings | None = None,
        partition_service: PartitionService | None = None,
    ) -> None:
        self._document_repo = document_repo
        self._workspace_repo = workspace_repo
        self._dispatcher = dispatcher
        self._config = config
        self._partition_service = partition_service

    # ------------------------------------------------------------------
    # Lookups (used by the thin router for its byte-identical guards)
    # ------------------------------------------------------------------

    async def file_exists(self, file_id: str, partition: str) -> bool:
        try:
            return await self._document_repo.file_exists_in_partition(
                file_id=file_id,
                partition=partition,
            )
        except Exception as e:  # pragma: no cover - defensive, matches legacy
            logger.exception("File existence check failed.", file_id=file_id, partition=partition, error=str(e))
            return False

    async def get_workspace(self, workspace_id: str) -> dict | None:
        return await self._workspace_repo.get_workspace_dict(workspace_id)

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def _build_metadata(
        self,
        *,
        metadata: dict,
        file_path: str,
        file_id: str,
        sanitized_filename: str,
        original_filename: str | None,
        content_sha256: str | None,
    ) -> dict:
        """Assemble the indexing metadata exactly as the legacy router did."""
        metadata = dict(metadata or {})
        metadata.update(
            {
                "source": str(file_path),
                "filename": sanitized_filename,
                "original_filename": original_filename,
            }
        )
        file_stat = Path(file_path).stat()
        metadata["file_size"] = _human_readable_size(file_stat.st_size)
        metadata["file_id"] = file_id
        metadata["content_sha256"] = content_sha256
        metadata.update(extract_temporal_fields(metadata, temporal_fields=TEMPORAL_FIELDS))
        return metadata

    def _partition_configs(self) -> dict[str, Any]:
        if self._config is None:
            return {}
        return getattr(self._config, "partitions", {}) or {}

    def _deduplication_enabled(self) -> bool:
        return bool(
            self._config is not None
            and getattr(getattr(self._config, "loader", None), "content_deduplication_enabled", False)
        )

    @asynccontextmanager
    async def _partition_admission(self, partition: str) -> AsyncIterator[bool]:
        if self._partition_service is None:
            yield False
            return

        admission = getattr(self._partition_service, "indexing_admission", None)
        if admission is not None:
            async with admission(partition) as partition_existed:
                yield bool(partition_existed)
            return

        yield await self._partition_service.partition_exists(partition)

    async def _ensure_editor_access_after_create_race(self, partition: str, user: dict | None) -> None:
        if user is None:
            return
        members = await self._partition_service.list_members(partition)
        membership = next((m for m in members if m.get("user_id") == user.get("id")), None)
        if membership is None or _ROLE_HIERARCHY.get(membership.get("role", ""), 0) < _ROLE_HIERARCHY["editor"]:
            # AuthError (status 403) so the global handler returns a clean
            # Forbidden; a builtin PermissionError would fall through to the
            # catch-all handler and surface as a 500.
            raise AuthError(f"Editor role required for partition: {partition}")

    def _resolve_indexation_dispatch_config(self, partition: str) -> tuple[dict | None, str | None]:
        partitions = self._partition_configs()
        if not partitions:
            return None, None
        if partition not in partitions:
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
        partition_cfg = partitions[partition]
        return partition_cfg.indexation.model_dump(mode="json"), partition_cfg.embedder

    async def _ensure_partition_exists(self, partition: str, user: dict | None) -> None:
        """Auto-create the partition on first index, matching legacy behaviour.

        Indexing into an unknown partition historically created it (with the
        uploader as owner) and then indexed. Phase 14J's per-partition
        resolution requires the partition to be present in ``config.partitions``,
        so create it here with default presets before resolving. No-op when
        there is no preset registry to resolve against (legacy passthrough) or
        no partition service wired.
        """
        if self._partition_service is None or not self._partition_configs():
            return
        if partition in self._partition_configs():
            return
        if await self._partition_service.partition_exists(partition):
            # Row exists but the in-memory cache is stale — refresh it.
            await self._partition_service.load_partitions()
            return
        user_id = (user or {}).get("id") or 1
        cap_user = user if user is not None else {"id": user_id, "is_admin": True}
        try:
            await self._partition_service.create_partition(
                partition,
                user_id=user_id,
                max_owned=max_partitions_for_user(cap_user),
            )
        except ValidationError as exc:
            if exc.code != "PARTITION_EXISTS":
                raise
            await self._partition_service.load_partitions()
            await self._ensure_editor_access_after_create_race(partition, user)
        else:
            logger.bind(partition=partition, user_id=user_id).info("Auto-created partition on index.")

    async def add_file(
        self,
        *,
        file_path: str,
        file_id: str,
        partition: str,
        metadata: dict,
        sanitized_filename: str,
        original_filename: str | None,
        user: dict | None,
        workspace_ids: list[str] | None = None,
        replace: bool = False,
        content_sha256: str | None = None,
    ) -> str:
        """Assemble metadata and queue an (re)indexing job; return its task id.

        Workspace association happens inside the worker's ``add_file``
        after a successful index — the router only pre-validates the ids.
        """
        if self._deduplication_enabled() and content_sha256 is None:
            content_sha256 = await asyncio.to_thread(_sha256_file, file_path)
        if not self._deduplication_enabled():
            content_sha256 = None

        full_metadata = self._build_metadata(
            metadata=metadata,
            file_path=file_path,
            file_id=file_id,
            sanitized_filename=sanitized_filename,
            original_filename=original_filename,
            content_sha256=content_sha256,
        )
        async with self._partition_admission(partition) as partition_existed_at_admission:
            await self._ensure_partition_exists(partition, user)
            require_existing_partition = bool(self._partition_configs()) or partition_existed_at_admission
            indexation_config, embedder_name = self._resolve_indexation_dispatch_config(partition)
            legacy_actor_preserves_partition_guard = require_existing_partition and indexation_config is not None
            return await self._dispatcher.dispatch_indexing(
                path=file_path,
                metadata=full_metadata,
                partition=partition,
                user=user,
                workspace_ids=workspace_ids,
                replace=replace,
                indexation_config=indexation_config,
                embedder_name=embedder_name,
                require_existing_partition=require_existing_partition,
                allow_legacy_require_existing_partition_retry=legacy_actor_preserves_partition_guard,
            )

    async def delete_file(self, file_id: str, partition: str) -> None:
        await self._dispatcher.delete_file(file_id, partition)

    async def update_metadata(
        self,
        file_id: str,
        metadata: dict,
        partition: str,
        user: dict | None,
    ) -> None:
        # Drop server-managed keys before they reach the store (#713). Without
        # this a partition editor could repoint ``source`` at another tenant's
        # upload under the shared data dir and read it back through
        # ``GET /static/{extract_id}`` — that route authorizes on the chunk's
        # partition, which is unchanged by the write. The upload path
        # (``dispatcher.dispatch_indexing``) and the MCP tools already filtered
        # these; only this REST path did not.
        metadata, dropped = strip_protected_metadata(metadata)
        if dropped:
            logger.bind(file_id=file_id, partition=partition).warning(
                f"Dropped protected metadata keys from file metadata update: {dropped}"
            )
        metadata["file_id"] = file_id
        await self._dispatcher.update_file_metadata(file_id, metadata, partition, user)

    async def copy_file(
        self,
        *,
        source_file_id: str,
        source_partition: str,
        target_file_id: str,
        target_partition: str,
        metadata: dict,
        user: dict | None,
    ) -> None:
        # Same guard as update_metadata (#713): the copy carries caller-supplied
        # metadata onto the new rows, so a spoofed ``source`` would land there too.
        metadata, dropped = strip_protected_metadata(metadata)
        if dropped:
            logger.bind(file_id=target_file_id, partition=target_partition).warning(
                f"Dropped protected metadata keys from file copy: {dropped}"
            )
        content_sha256 = None
        if self._deduplication_enabled():
            content_sha256 = await self._document_repo.get_content_sha256(source_file_id, source_partition)
        metadata["file_id"] = target_file_id
        metadata["partition"] = target_partition
        metadata["content_sha256"] = content_sha256
        await self._dispatcher.copy_file(source_file_id, metadata, source_partition, user)

    # ------------------------------------------------------------------
    # Task state
    # ------------------------------------------------------------------

    async def get_task_state(self, task_id: str) -> str | None:
        return await self._dispatcher.get_task_state(task_id)

    async def get_task_error(self, task_id: str) -> str | None:
        return await self._dispatcher.get_task_error(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        return await self._dispatcher.cancel_task(task_id)


__all__ = ["IndexingService"]
