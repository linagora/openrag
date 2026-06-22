from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from core.models.document import Document
from services.workers.parsers.doc_serializer_bridge import INDEXATION_CONFIG_METADATA_KEY
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
    ) -> None:
        self._pipeline = pipeline
        self._tsm = task_state_manager
        self._document_repo = document_repo
        self._topic_tag_repo = topic_tag_repo

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
            document = _load_document(path, metadata, partition, indexation_config=indexation_config)
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


def _load_document(
    path: str,
    metadata: dict[str, Any],
    partition: str,
    *,
    indexation_config: dict[str, Any] | None = None,
) -> Document:
    p = Path(path)
    document_metadata = dict(metadata)
    if indexation_config is not None:
        document_metadata[INDEXATION_CONFIG_METADATA_KEY] = dict(indexation_config)
    return Document(
        filename=metadata.get("file_id") or p.name,
        raw_bytes=p.read_bytes(),
        content_type=Document.detect_content_type(p.name),
        partition=partition,
        metadata=document_metadata,
    )


__all__ = ["IndexerWorker"]
