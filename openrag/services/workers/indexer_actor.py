from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from core.models.document import Document
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
    ) -> None:
        self._pipeline = pipeline
        self._tsm = task_state_manager
        self._document_repo = document_repo

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
            document = _load_document(path, metadata, partition)
            row: dict[str, Any] = {
                "document": document,
                "partition": partition,
                "filename": Path(path).name,
                "language": metadata.get("language", "en"),
                "replace": replace,
                "user": user,
                "workspace_ids": workspace_ids,
                "indexation_config": indexation_config,
                "embedder_name": embedder_name,
            }
            await self._pipeline.run(row)
            if self._document_repo is not None:
                await _write_catalog_record(
                    doc_repo=self._document_repo,
                    metadata=metadata,
                    partition=partition,
                    user=user,
                    replace=replace,
                    indexation_config=indexation_config,
                )
            await self._tsm.set_state.remote(task_id, "COMPLETED")
            return {"stored_count": row.get("stored_count", 0), "stage": row.get("stage", "")}
        except Exception:
            tb = traceback.format_exc()
            await self._tsm.set_failed_if_not_cancelled.remote(task_id, tb)
            raise


async def _write_catalog_record(
    *,
    doc_repo: Any,
    metadata: dict[str, Any],
    partition: str,
    user: dict[str, Any] | None,
    replace: bool,
    indexation_config: dict[str, Any] | None,
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
        **config_kwargs,
    )


def _load_document(
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
    return Document(
        id=file_id,
        filename=file_id,
        raw_bytes=p.read_bytes(),
        content_type=Document.detect_content_type(p.name),
        partition=partition,
        metadata=dict(metadata),
    )


__all__ = ["IndexerWorker"]
