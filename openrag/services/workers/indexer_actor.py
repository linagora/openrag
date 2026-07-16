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

    Since #664 the worker is also the owner of the uploader's reserved
    quota slot: when the caller admitted the upload it already charged one
    ``users.file_count`` slot, and this worker either consumes it (by
    writing the catalog row) or releases it — see ``process_file``.
    """

    def __init__(
        self,
        pipeline: IndexingPipeline,
        task_state_manager: Any,
        document_repo: Any = None,
        topic_tag_repo: Any = None,
        vector_store: Any = None,
        collection: str = "default",
        user_repo: Any = None,
    ) -> None:
        self._pipeline = pipeline
        self._tsm = task_state_manager
        self._document_repo = document_repo
        self._topic_tag_repo = topic_tag_repo
        self._vector_store = vector_store
        self._collection = collection
        self._user_repo = user_repo

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
        quota_reserved: bool = False,
    ) -> dict[str, Any]:
        """Run one file through the indexing pipeline.

        Returns a plain dict ``{"stored_count": int, "stage": "stored"}``
        on success.  On failure, state is set to FAILED and the exception
        is re-raised so the Ray task is marked as errored.

        When ``quota_reserved`` is set, the admission gate already charged
        one ``users.file_count`` slot to the uploader (#664) and this call
        owns it. The slot is *consumed* the moment ``add_file_to_partition``
        reports a new catalog row; on every other outcome — pipeline
        failure, cancellation, or the duplicate-at-catalog race where the
        insert reports ``False`` — the ``finally`` below hands it back, so a
        rejected upload can never permanently eat a slot.
        """
        # Released in ``finally`` unless the catalog write claims it. A flag
        # plus ``finally`` (rather than an ``except``) is what makes
        # cancellation safe: ray.cancel raises ``asyncio.CancelledError``, a
        # BaseException that ``except Exception`` would sail straight past.
        release_slot = bool(quota_reserved)
        user_id = (user or {}).get("id")
        await self._tsm.set_state.remote(task_id, "SERIALIZING")
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
                if not replace:
                    # A *new* catalog row now exists for this reservation, so it
                    # is consumed; releasing it would hand the user free quota.
                    # ``replace`` reuses an existing row and creates nothing, but
                    # it also never reserves (only ``add_file`` does), so there is
                    # no slot to consume on that branch.
                    #
                    # The duplicate-at-catalog race no longer reaches here at all:
                    # ``add_file_to_partition`` returns False for an existing row,
                    # which the fail-closed check above turns into a raise, and
                    # ``process_file``'s ``finally`` releases the slot on the way
                    # out. Same outcome as the pre-rebase ``if created:`` gate,
                    # reached by a different path.
                    release_slot = False
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
            await self._tsm.set_failed_if_not_cancelled.remote(task_id, tb)
            raise
        finally:
            if release_slot:
                await _release_quota_slot(self._user_repo, user_id)
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
    """Write the file's catalog row; return whether the write landed.

    True means the catalog now holds a row for this task -- an updated one on a
    ``replace`` re-index, a newly inserted one otherwise. False means the write
    did not land, which the caller treats as fatal.

    Note this is *not* "a new file was created". Before the rebase onto #671
    this returned that instead, so a ``replace`` reported False and the caller
    read it as "reservation unconsumed". #671 added a fail-closed
    ``if not wrote_catalog: raise`` on the same value, which those False returns
    would have turned into a hard failure of every ``replace`` re-index. The two
    questions are now separate: this one reports the write, and the caller
    derives the quota verdict from ``replace`` (#664).
    """
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


async def _release_quota_slot(user_repo: Any, user_id: int | None) -> None:
    """Give the uploader's reserved file slot back, swallowing any error.

    Runs on cleanup paths only. A release that raises would replace the real
    indexing error with a database error, so failures are swallowed — but
    loudly, because a lost release leaves ``file_count`` one too high and
    permanently narrows that user's quota until an admin reconciles it.
    """
    if user_repo is None or user_id is None:
        return
    try:
        await user_repo.release_file_slot(user_id)
    except Exception:  # noqa: BLE001 - cleanup must never mask the indexing error
        import logging

        logging.getLogger(__name__).exception(
            "Failed to release reserved file slot for user %s; file_count is now one too high.",
            user_id,
        )


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
