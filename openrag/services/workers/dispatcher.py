from __future__ import annotations

import traceback
import uuid
from typing import Any

from core.indexing.dispatcher import IndexingDispatcher
from core.utils.logging import get_logger
from ray.exceptions import TaskCancelledError
from services.workers.ray_utils import call_ray_actor_with_timeout
from services.workers.task_cancellation import cancel_active_indexing_tasks

logger = get_logger()

DEFAULT_TIMEOUT = 60.0


class WorkerDispatcher(IndexingDispatcher):
    """Dispatcher that routes new indexing jobs through ``IndexerPool``.

    File mutation paths use the storage ports directly so the API no longer
    depends on the legacy ``Indexer`` actor being present.
    """

    _FILE_METADATA_EXCLUDED_KEYS = frozenset(
        {
            "_id",
            "id",
            "text",
            "vector",
            "page",
            "section_id",
            "prev_section_id",
            "next_section_id",
        }
    )
    _INTERNAL_METADATA_PREFIX = "_openrag"

    def __init__(
        self,
        *,
        pool: Any,
        task_state_manager: Any,
        vector_store: Any,
        document_repo: Any,
        workspace_repo: Any,
        collection: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._pool = pool
        self._tsm = task_state_manager
        self._vector_store = vector_store
        self._document_repo = document_repo
        self._workspace_repo = workspace_repo
        self._collection = collection
        self._timeout = timeout

    async def _call(self, future: Any, task_description: str) -> Any:
        from services.workers.ray_utils import call_ray_actor_with_timeout

        return await call_ray_actor_with_timeout(
            future=future,
            timeout=self._timeout,
            task_description=task_description,
        )

    async def dispatch_indexing(
        self,
        *,
        path: str,
        metadata: dict,
        partition: str,
        user: dict | None,
        workspace_ids: list[str] | None,
        replace: bool,
        indexation_config: dict | None = None,
        embedder_name: str | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex

        await self._call(
            self._tsm.set_state.remote(task_id, "QUEUED"),
            task_description=f"set_state({task_id})",
        )

        user_metadata = {key: value for key, value in metadata.items() if key not in {"file_id", "source"}}
        await self._call(
            self._tsm.set_details.remote(
                task_id,
                file_id=metadata.get("file_id"),
                partition=partition,
                metadata=user_metadata,
                user_id=user.get("id") if user else None,
            ),
            task_description=f"set_details({task_id})",
        )

        task: Any | None = None
        try:
            # ``IndexerPool`` is a Ray actor; ``submit`` returns ``[worker_ref]``
            # (wrapped so Ray doesn't auto-dereference and block on the worker task).
            # Awaiting the submit call yields that list; element 0 is the worker ref
            # that ``cancel_task``/``ray.cancel`` must target.
            submitted = await self._call(
                self._pool.submit.remote(
                    task_id=task_id,
                    path=path,
                    metadata=metadata,
                    partition=partition,
                    user=user,
                    workspace_ids=workspace_ids,
                    replace=replace,
                    indexation_config=indexation_config,
                    embedder_name=embedder_name,
                ),
                task_description=f"submit({task_id})",
            )
            task = submitted[0]

            registered = await self._call(
                self._tsm.set_object_ref.remote(task_id, {"ref": task}),
                task_description=f"set_object_ref({task_id})",
            )
            if registered is False:
                raise RuntimeError(f"Task {task_id} was cancelled before worker ref registration")
        except Exception:
            if task is not None:
                await self._cancel_submitted_task(task_id, task)
            await self._mark_submit_failed(task_id, traceback.format_exc())
            raise
        return task_id

    async def _mark_submit_failed(self, task_id: str, tb: str) -> None:
        set_failed = getattr(self._tsm, "set_failed_if_not_cancelled", None)
        if set_failed is not None:
            await self._call(
                set_failed.remote(task_id, tb),
                task_description=f"set_failed_if_not_cancelled({task_id})",
            )
            return
        await self._call(
            self._tsm.set_state.remote(task_id, "FAILED"),
            task_description=f"set_state({task_id}, FAILED)",
        )

    async def _cancel_submitted_task(self, task_id: str, task: Any) -> None:
        import ray

        try:
            ray.cancel(task, recursive=True)
        except Exception as exc:
            logger.warning(
                "Failed to cancel submitted indexing task after dispatch failure",
                task_id=task_id,
                error=str(exc),
            )
            return
        try:
            await call_ray_actor_with_timeout(
                future=task,
                timeout=self._timeout,
                task_description=f"cancel_submitted_task({task_id})",
            )
        except TaskCancelledError:
            return
        except TimeoutError:
            logger.warning(
                "Timed out waiting for submitted indexing task to settle after dispatch failure",
                task_id=task_id,
            )
        except Exception as exc:
            logger.info(
                "Submitted indexing task settled after dispatch failure cancellation request",
                task_id=task_id,
                error=str(exc),
            )

    async def delete_file(self, file_id: str, partition: str) -> None:
        cancelled = await cancel_active_indexing_tasks(
            self._tsm,
            partition=partition,
            file_id=file_id,
            timeout=self._timeout,
        )
        if cancelled:
            logger.info("Cancelled active indexing tasks before deleting file", file_id=file_id, partition=partition)

        collection_exists = await self._vector_store.collection_exists(self._collection)
        if collection_exists:
            await self._vector_store.delete_by_filter({"partition": partition, "file_id": file_id})
        await self._workspace_repo.remove_file_from_all_workspaces(file_id, partition)
        await self._document_repo.remove_file_from_partition(file_id=file_id, partition=partition)
        if collection_exists:
            try:
                await self._vector_store.delete_by_filter({"partition": partition, "file_id": file_id})
            except Exception as exc:
                logger.warning(
                    "Post-delete vector cleanup failed after file catalog removal",
                    file_id=file_id,
                    partition=partition,
                    error=str(exc),
                )
                raise

    async def update_file_metadata(
        self,
        file_id: str,
        metadata: dict,
        partition: str,
        user: dict | None,
    ) -> None:
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
            output_fields=["*", "vector"],
        )
        if not rows:
            return

        entities = []
        for row in rows:
            entity = self._strip_internal_metadata(row)
            entity.update(metadata)
            entities.append(entity)

        await self._upsert_entities(entities)

        file_metadata = self._file_metadata_from_chunk(rows[0])
        file_metadata.update(metadata)
        await self._document_repo.update_file_metadata_in_db(file_id, partition, file_metadata)

    async def copy_file(
        self,
        file_id: str,
        metadata: dict,
        partition: str,
        user: dict | None,
    ) -> None:
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
            output_fields=["*", "vector"],
        )
        if not rows:
            return

        entities = []
        for row in rows:
            entity = self._strip_internal_metadata(row)
            entity.pop("_id", None)
            entity.update(metadata)
            entities.append(entity)

        await self._insert_entities(entities)

        target_file_id = metadata.get("file_id", file_id)
        target_partition = metadata.get("partition", partition)
        file_metadata = self._file_metadata_from_chunk(rows[0])
        file_metadata.update(metadata)
        await self._document_repo.add_file_to_partition(
            file_id=target_file_id,
            partition=target_partition,
            file_metadata=file_metadata,
            user_id=user.get("id") if user else None,
            relationship_id=file_metadata.get("relationship_id"),
            parent_id=file_metadata.get("parent_id"),
        )

    async def _upsert_entities(self, entities: list[dict[str, Any]]) -> None:
        upsert_entities = getattr(self._vector_store, "upsert_entities", None)
        if upsert_entities is None:
            raise TypeError("vector_store must expose upsert_entities for file metadata mutations")
        await upsert_entities(entities, self._collection)

    async def _insert_entities(self, entities: list[dict[str, Any]]) -> None:
        insert_entities = getattr(self._vector_store, "insert_entities", None)
        if insert_entities is None:
            raise TypeError("vector_store must expose insert_entities for file copy mutations")
        await insert_entities(entities, self._collection)

    def _file_metadata_from_chunk(self, chunk: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v
            for k, v in chunk.items()
            if k not in self._FILE_METADATA_EXCLUDED_KEYS and not self._is_internal_metadata_key(k)
        }

    @classmethod
    def _strip_internal_metadata(cls, row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if not cls._is_internal_metadata_key(k)}

    @classmethod
    def _is_internal_metadata_key(cls, key: Any) -> bool:
        return isinstance(key, str) and key.startswith(cls._INTERNAL_METADATA_PREFIX)

    async def get_task_state(self, task_id: str) -> str | None:
        return await self._call(
            self._tsm.get_state.remote(task_id),
            task_description=f"get_state({task_id})",
        )

    async def get_task_error(self, task_id: str) -> str | None:
        return await self._call(
            self._tsm.get_error.remote(task_id),
            task_description=f"get_error({task_id})",
        )

    async def cancel_task(self, task_id: str) -> bool:
        import ray

        obj_ref = await self._call(
            self._tsm.get_object_ref.remote(task_id),
            task_description=f"get_object_ref({task_id})",
        )
        if obj_ref is None:
            return False

        ray.cancel(obj_ref["ref"], recursive=True)
        await self._call(
            self._tsm.set_state.remote(task_id, "CANCELLED"),
            task_description=f"set_state({task_id})",
        )
        return True


def from_ray_namespace(
    namespace: str = "openrag",
    timeout: float = DEFAULT_TIMEOUT,
    *,
    vector_store: Any,
    document_repo: Any,
    workspace_repo: Any,
    collection: str,
) -> WorkerDispatcher:
    import ray
    from services.workers.indexer_pool import build_indexer_pool

    return WorkerDispatcher(
        pool=build_indexer_pool(namespace=namespace),
        task_state_manager=ray.get_actor("TaskStateManager", namespace=namespace),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection=collection,
        timeout=timeout,
    )


__all__ = ["WorkerDispatcher", "from_ray_namespace"]
