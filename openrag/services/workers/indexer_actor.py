from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from core.models.document import Document
from core.utils.logging import get_logger
from services.workers.pipeline_builder import (
    REPLACE_OLD_CHUNK_COLLECTION_ROW_KEY,
    REPLACE_OLD_CHUNK_IDS_ROW_KEY,
    IndexingPipeline,
)
from services.workers.ray_utils import retry_idempotent_ray_actor_method
from services.workers.stages._common import run_with_optional_timeout
from services.workers.stages.store import INDEXING_TASK_ID_METADATA_KEY

logger = get_logger()


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
        vector_store: Any = None,
        collection: str = "default",
    ) -> None:
        self._pipeline = pipeline
        self._tsm = task_state_manager
        self._document_repo = document_repo
        self._topic_tag_repo = topic_tag_repo
        self._vector_store = vector_store
        self._collection = collection

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
        require_existing_partition: bool = False,
        resolved_prompts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run one file through the indexing pipeline.

        Returns a plain dict ``{"stored_count": int, "stage": "stored"}``
        on success.  On failure, state is set to FAILED and the exception
        is re-raised so the Ray task is marked as errored.
        """
        accepted = await retry_idempotent_ray_actor_method(
            lambda: self._tsm.set_state.remote(task_id, "SERIALIZING"),
            task_description=f"set_state({task_id}, SERIALIZING)",
        )
        if accepted is False:
            raise RuntimeError(f"Task {task_id} was cancelled before indexing started")
        row: dict[str, Any] | None = None
        catalog_written = False
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
            # DB-resolved enrichment prompts (contextualizer/topic-tagger/caption)
            # for this partition; each stage prefers its row value over the
            # process-wide disk default. Absent keys leave the disk fallback.
            if resolved_prompts:
                row.update(resolved_prompts)
            row = await self._pipeline.run(row)
            indexed_at = row.get("indexed_at")

            if self._document_repo is not None:
                wrote_catalog = await _write_catalog_record(
                    doc_repo=self._document_repo,
                    metadata=metadata,
                    partition=partition,
                    user=user,
                    replace=replace,
                    indexation_config=indexation_config,
                    indexed_at=indexed_at,
                    require_existing_partition=require_existing_partition,
                )
                if not wrote_catalog:
                    raise RuntimeError("Catalog row was not written after vector indexing")
                catalog_written = True
                await _delete_replaced_chunks_after_catalog(
                    vector_store=self._vector_store or getattr(self._pipeline, "vector_store", None),
                    row=row,
                    timeout=getattr(getattr(self._pipeline, "timeouts", None), "store", None),
                )
            if self._topic_tag_repo is not None:
                await _replace_topic_tags_if_needed(
                    topic_tag_repo=self._topic_tag_repo,
                    row=row,
                    metadata=metadata,
                    partition=partition,
                    indexation_config=indexation_config,
                )
            await retry_idempotent_ray_actor_method(
                lambda: self._tsm.set_state.remote(task_id, "COMPLETED"),
                task_description=f"set_state({task_id}, COMPLETED)",
            )
            return {"stored_count": row.get("stored_count", 0), "stage": row.get("stage", "")}
        except Exception:
            should_cleanup_vectors = row is not None and (
                row.get("stored_count", 0) > 0 or row.get("stage") == "store_failed"
            )
            if not catalog_written and should_cleanup_vectors:
                await _cleanup_indexed_vectors(
                    vector_store=self._vector_store or getattr(self._pipeline, "vector_store", None),
                    collection=self._collection,
                    metadata=metadata,
                    partition=partition,
                    task_id=task_id,
                )
            tb = traceback.format_exc()
            await retry_idempotent_ray_actor_method(
                lambda: self._tsm.set_failed_if_not_cancelled.remote(task_id, tb),
                task_description=f"set_failed_if_not_cancelled({task_id})",
            )
            raise
        # The raw upload is purged (when configured) by the enclosing actor, not
        # here: cleanup must also cover failures that happen *before* this method
        # runs (catalog/registry init, the SERIALIZING state update). See
        # ``delete_uploaded_file`` and ``IndexerWorkerActor.process_file``.


async def _write_catalog_record(
    *,
    doc_repo: Any,
    metadata: dict[str, Any],
    partition: str,
    user: dict[str, Any] | None,
    replace: bool,
    indexation_config: dict[str, Any] | None,
    indexed_at: datetime | None = None,
    require_existing_partition: bool = False,
) -> bool:
    file_id = metadata.get("file_id", "")
    file_metadata = {key: value for key, value in metadata.items() if key != "page"}
    config_kwargs = {"indexation_config": indexation_config} if indexation_config is not None else {}
    if replace:
        return await doc_repo.update_file_in_partition(
            file_id=file_id,
            partition=partition,
            file_metadata=file_metadata,
            relationship_id=metadata.get("relationship_id"),
            parent_id=metadata.get("parent_id"),
            indexed_at=indexed_at,
            content_sha256=metadata.get("content_sha256"),
            **config_kwargs,
        )

    return await doc_repo.add_file_to_partition(
        file_id=file_id,
        partition=partition,
        file_metadata=file_metadata,
        user_id=user.get("id") if user else None,
        relationship_id=metadata.get("relationship_id"),
        parent_id=metadata.get("parent_id"),
        indexed_at=indexed_at,
        require_existing_partition=require_existing_partition,
        content_sha256=metadata.get("content_sha256"),
        **config_kwargs,
    )


async def _cleanup_indexed_vectors(
    *,
    vector_store: Any,
    collection: str,
    metadata: dict[str, Any],
    partition: str,
    task_id: str,
) -> None:
    file_id = metadata.get("file_id")
    if vector_store is None or not file_id or not task_id:
        return
    try:
        if await vector_store.collection_exists(collection):
            await vector_store.delete_by_filter(
                {
                    "partition": partition,
                    "file_id": file_id,
                    INDEXING_TASK_ID_METADATA_KEY: str(task_id),
                }
            )
    except Exception:
        # Preserve the original indexing failure. A later file delete still runs
        # the broader partition/file cleanup path if this best-effort sweep fails.
        return


async def _delete_replaced_chunks_after_catalog(
    *,
    vector_store: Any,
    row: dict[str, Any],
    timeout: float | None,
) -> None:
    ids = row.get(REPLACE_OLD_CHUNK_IDS_ROW_KEY)
    if vector_store is None or not isinstance(ids, list) or not ids:
        return
    collection = row.get(REPLACE_OLD_CHUNK_COLLECTION_ROW_KEY) or "default"
    try:
        deleted = await run_with_optional_timeout(lambda: vector_store.delete(ids, collection), timeout)
        logger.bind(task_id=row.get("task_id")).debug(f"re-index: removed {deleted} stale chunk(s) after replace")
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort after catalog commit
        logger.bind(task_id=row.get("task_id")).error(
            f"re-index: catalog was updated but failed to delete {len(ids)} stale chunk(s); "
            f"duplicates remain until reconciliation: {exc}"
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
    # Mirrors IndexationPipelineConfig.enable_topic_tagging: an absent key means
    # disabled, so a re-index under a preset that never enabled tagging still
    # clears tags left behind by an earlier run.
    topic_tagging_disabled = indexation_config is not None and not indexation_config.get("enable_topic_tagging", False)
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


async def delete_uploaded_file(path: str, logger: Any) -> None:
    """Remove the raw upload from disk, swallowing any cleanup error.

    Called from the actor boundary (``IndexerWorkerActor.process_file``) when
    ``save_uploaded_files`` is off, so a client that manages its own files never
    has a disk copy left behind — even when indexing fails before the pipeline
    runs. A failed delete must never turn indexing into a failure, so the error
    is logged and discarded.
    """
    try:
        await asyncio.to_thread(Path(path).unlink, missing_ok=True)
        logger.debug(f"Deleted input file: {path}")
    except Exception as cleanup_err:  # noqa: BLE001 - cleanup must not fail the task
        logger.warning(f"Failed to delete input file {path}: {cleanup_err}")


__all__ = ["IndexerWorker", "delete_uploaded_file"]
