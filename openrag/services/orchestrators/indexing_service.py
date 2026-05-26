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

from pathlib import Path
from typing import TYPE_CHECKING

from components.indexer.utils.files import extract_temporal_fields
from utils.logger import get_logger

if TYPE_CHECKING:
    from core.indexing.dispatcher import IndexingDispatcher
    from core.ports.document_repo import DocumentRepository
    from core.ports.workspace_repo import WorkspaceRepository

logger = get_logger()

# Client-supplied datetime fields lifted into queryable metadata.
TEMPORAL_FIELDS = ["created_at"]


def _human_readable_size(size_bytes: int) -> str:
    """Bytes → human-readable string (e.g. ``'2.40 MB'``).

    Kept private here rather than imported from ``routers/utils.py`` —
    services must not depend on the HTTP layer.
    """
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


class IndexingService:
    """File upload/delete/copy/metadata orchestration over the worker layer."""

    def __init__(
        self,
        *,
        document_repo: DocumentRepository,
        workspace_repo: WorkspaceRepository,
        dispatcher: IndexingDispatcher,
    ) -> None:
        self._document_repo = document_repo
        self._workspace_repo = workspace_repo
        self._dispatcher = dispatcher

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
        metadata.update(extract_temporal_fields(metadata, temporal_fields=TEMPORAL_FIELDS))
        return metadata

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
    ) -> str:
        """Assemble metadata and queue an (re)indexing job; return its task id.

        Workspace association happens inside the worker's ``add_file``
        after a successful index — the router only pre-validates the ids.
        """
        full_metadata = self._build_metadata(
            metadata=metadata,
            file_path=file_path,
            file_id=file_id,
            sanitized_filename=sanitized_filename,
            original_filename=original_filename,
        )
        return await self._dispatcher.dispatch_indexing(
            path=file_path,
            metadata=full_metadata,
            partition=partition,
            user=user,
            workspace_ids=workspace_ids,
            replace=replace,
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
        metadata = dict(metadata or {})
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
        metadata = dict(metadata or {})
        metadata["file_id"] = target_file_id
        metadata["partition"] = target_partition
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
