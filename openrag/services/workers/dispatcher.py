from __future__ import annotations

import uuid
from typing import Any

from core.indexing.dispatcher import IndexingDispatcher

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

        task = self._pool.process_file.remote(
            task_id=task_id,
            path=path,
            metadata=metadata,
            partition=partition,
            user=user,
            workspace_ids=workspace_ids,
            replace=replace,
        )

        await self._call(
            self._tsm.set_object_ref.remote(task_id, {"ref": task}),
            task_description=f"set_object_ref({task_id})",
        )
        return task_id

    async def delete_file(self, file_id: str, partition: str) -> None:
        ids = await self._vector_store.query_ids_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
        )
        if ids:
            await self._vector_store.delete(ids, self._collection)
        await self._workspace_repo.remove_file_from_all_workspaces(file_id, partition)
        await self._document_repo.remove_file_from_partition(file_id=file_id, partition=partition)

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
            entity = dict(row)
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
            entity = dict(row)
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
        return {k: v for k, v in chunk.items() if k not in self._FILE_METADATA_EXCLUDED_KEYS}

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
