from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from core.models.document import Document
from core.utils.logging import get_logger
from services.workers.pipeline_builder import IndexingPipeline


class IndexerWorker:
    """Pure-Python core of the thin indexer actor.

    ``@ray.remote`` is not applied here so the class is directly
    instantiable in tests.  The production Ray actor wraps this class
    (or applies ``@ray.remote`` at startup).

    State transitions reported to *task_state_manager* are compatible
    with the existing queue-monitoring states:

    ``SERIALIZING`` — processing has started (parse + chunk + embed + store)
    ``COMPLETED``   — pipeline finished successfully
    ``FAILED``      — pipeline raised; set via ``set_failed_if_not_cancelled``

    Callers are responsible for setting ``QUEUED`` *before* dispatching
    the task, and for storing the object ref via ``set_object_ref``.
    """

    def __init__(
        self,
        pipeline: IndexingPipeline,
        task_state_manager: Any,
        document_repo: Any = None,
        topic_tag_repo: Any = None,
        save_uploaded_files: bool = True,
    ) -> None:
        self._pipeline = pipeline
        self._tsm = task_state_manager
        self._document_repo = document_repo
        self._topic_tag_repo = topic_tag_repo
        # When False (e.g. Twake, which keeps its own copy), the raw upload is
        # removed from ``paths.data_dir`` once indexing settles so only the
        # derived chunks are retained.
        self._save_uploaded_files = save_uploaded_files
        # Instance-level logger (not module-global): the enclosing actor pickles
        # by value, and a module-global loguru handle would make it unpicklable.
        self._logger = get_logger()

    async def process_file(
        self,
        *,
        task_id: str,
        path: str,
        metadata: dict[str, Any],
        partition: str,
        user: dict[str, Any] | None = None,
        workspace_ids: list[str] | None = None,
        replace: bool = False,
        indexation_config: dict[str, Any] | None = None,
        embedder_name: str | None = None,
    ) -> dict[str, Any]:
        """Run one file through the indexing pipeline.

        Returns a plain dict ``{"stored_count": int, "stage": "stored"}``
        on success.  On failure, state is set to FAILED and the exception
        is re-raised so the Ray task is marked as errored.
        """
        await self._tsm.set_state.remote(task_id, "SERIALIZING")
        try:
            document = await _load_document(path, metadata, partition)
            # One indexation timestamp for this file, shared by the Milvus chunks
            # (via the store stage) and the Postgres catalog row, so they agree.
            row: dict[str, Any] = {
                "task_id": task_id,
                "document": document,
                "partition": partition,
                "filename": document.filename,
                "language": metadata.get("language", "en"),
                "replace": replace,
                "user": user,
                "workspace_ids": workspace_ids,
                "indexation_config": indexation_config,
                "embedder_name": embedder_name,
            }
            row = await self._pipeline.run(row)
            indexed_at = row.get("indexed_at")

            if self._document_repo is not None:
                await _write_catalog_record(
                    doc_repo=self._document_repo,
                    metadata=metadata,
                    partition=partition,
                    user=user,
                    replace=replace,
                    indexation_config=indexation_config,
                    indexed_at=indexed_at,
                )
            if self._topic_tag_repo is not None:
                await _replace_topic_tags_if_needed(
                    topic_tag_repo=self._topic_tag_repo,
                    row=row,
                    metadata=metadata,
                    partition=partition,
                    indexation_config=indexation_config,
                )
            await self._tsm.set_state.remote(task_id, "COMPLETED")
            return {"stored_count": row.get("stored_count", 0), "stage": row.get("stage", "")}
        except Exception:
            tb = traceback.format_exc()
            await self._tsm.set_failed_if_not_cancelled.remote(task_id, tb)
            raise
        finally:
            # Cleanup runs on both success and failure: a client that manages its
            # own files wants the disk copy gone regardless of how indexing ended.
            if not self._save_uploaded_files:
                await self._delete_input_file(path)

    async def _delete_input_file(self, path: str) -> None:
        """Remove the raw upload from disk, swallowing any cleanup error.

        A failed delete must never turn a successful indexation into a failure,
        so the exception is logged and discarded.
        """
        try:
            await asyncio.to_thread(Path(path).unlink, missing_ok=True)
            self._logger.debug(f"Deleted input file: {path}")
        except Exception as cleanup_err:  # noqa: BLE001 - cleanup must not fail the task
            self._logger.warning(f"Failed to delete input file {path}: {cleanup_err}")


async def _write_catalog_record(
    *,
    doc_repo: Any,
    metadata: dict[str, Any],
    partition: str,
    user: dict[str, Any] | None,
    replace: bool,
    indexation_config: dict[str, Any] | None,
    indexed_at: datetime | None = None,
) -> None:
    file_id = metadata.get("file_id", "")
    file_metadata = {key: value for key, value in metadata.items() if key != "page"}
    config_kwargs = {"indexation_config": indexation_config} if indexation_config is not None else {}
    if replace:
        await doc_repo.update_file_in_partition(
            file_id=file_id,
            partition=partition,
            file_metadata=file_metadata,
            relationship_id=metadata.get("relationship_id"),
            parent_id=metadata.get("parent_id"),
            indexed_at=indexed_at,
            **config_kwargs,
        )
        return

    await doc_repo.add_file_to_partition(
        file_id=file_id,
        partition=partition,
        file_metadata=file_metadata,
        user_id=user.get("id") if user else None,
        relationship_id=metadata.get("relationship_id"),
        parent_id=metadata.get("parent_id"),
        indexed_at=indexed_at,
        **config_kwargs,
    )


async def _replace_topic_tags_if_needed(
    *,
    topic_tag_repo: Any,
    row: dict[str, Any],
    metadata: dict[str, Any],
    partition: str,
    indexation_config: dict[str, Any] | None,
) -> None:
    file_id = metadata.get("file_id", "")
    if not file_id:
        return

    has_topic_tags = "topic_tags" in row
    topic_tagging_disabled = indexation_config is not None and indexation_config.get("enable_topic_tagging") is False
    if not has_topic_tags and not topic_tagging_disabled:
        return

    raw_tags = row.get("topic_tags", [])
    if not isinstance(raw_tags, list):
        raise TypeError("topic_tags must be a list of strings")

    tags = [tag for tag in raw_tags if isinstance(tag, str) and tag.strip()]
    await topic_tag_repo.delete_by_document(file_id, partition=partition)
    if not tags:
        return
    await topic_tag_repo.bulk_insert(
        [
            {
                "document_id": file_id,
                "partition": partition,
                "tag": tag,
            }
            for tag in tags
        ]
    )


async def _load_document(
    path: str,
    metadata: dict[str, Any],
    partition: str,
) -> Document:
    p = Path(path)
    file_id = metadata.get("file_id")
    if not file_id:
        # file_id is a required route path param, force-set by
        # IndexingService._build_metadata. Missing here means a broken upstream
        # contract — fail loudly rather than silently persisting chunks under a
        # non-queryable id (e.g. the temp upload's basename).
        raise ValueError("_load_document requires metadata['file_id']")
    # ``Document.id`` is the file's identity, not a random uuid: parsers set
    # ``ProcessedDocument.document_id = document.id`` and the chunker uses that as
    # ``Chunk.document_id`` / ``file_id``. If this defaulted to uuid4, chunks would
    # persist under an id the ``/partition/{partition}/file/{file_id}`` lookup
    # never queries by (zero chunks found).
    #
    # Per-partition indexation_config reaches the pipeline via ``row["indexation_config"]``
    # (see IndexerWorker.process_file); it is intentionally not stamped into the
    # document metadata so it never leaks into chunk metadata.
    filename = _display_filename(path, metadata)
    raw_bytes = await asyncio.to_thread(p.read_bytes)
    return Document(
        id=file_id,
        filename=filename,
        raw_bytes=raw_bytes,
        content_type=Document.detect_content_type(filename),
        partition=partition,
        metadata=dict(metadata),
    )


def _display_filename(path: str, metadata: dict[str, Any]) -> str:
    """Return the user-facing filename while falling back to the stored path."""

    filename = metadata.get("original_filename") or metadata.get("filename")
    if filename:
        return str(filename)
    return Path(path).name


__all__ = ["IndexerWorker"]
