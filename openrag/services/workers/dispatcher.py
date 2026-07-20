from __future__ import annotations

import traceback
import uuid
from typing import Any

from core.indexing.dispatcher import IndexingDispatcher
from core.utils.conts import is_internal_metadata_key, strip_internal_metadata
from core.utils.logging import get_logger
from ray.exceptions import TaskCancelledError
from services.workers.ray_utils import call_ray_actor_with_timeout
from services.workers.task_cancellation import cancel_active_indexing_tasks

logger = get_logger()

DEFAULT_TIMEOUT = 60.0
_REQUIRE_EXISTING_PARTITION_KWARG = "require_existing_partition"


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
        indexation_config: dict | None = None,
        embedder_name: str | None = None,
        require_existing_partition: bool = False,
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
            submit_kwargs: dict[str, Any] = {
                "task_id": task_id,
                "path": path,
                "metadata": metadata,
                "partition": partition,
                "user": user,
                "workspace_ids": workspace_ids,
                "replace": replace,
                "indexation_config": indexation_config,
                "embedder_name": embedder_name,
            }
            if require_existing_partition:
                submit_kwargs[_REQUIRE_EXISTING_PARTITION_KWARG] = True
            task = await self._submit_indexing_task(
                task_id,
                submit_kwargs,
                allow_legacy_retry=require_existing_partition,
            )

            registered = await self._call(
                self._tsm.set_object_ref.remote(task_id, {"ref": task}),
                task_description=f"set_object_ref({task_id})",
            )
            if registered is False:
                raise RuntimeError(f"Task {task_id} was cancelled before worker ref registration")
        except Exception:
            mark_submit_failed = True
            if task is not None:
                mark_submit_failed = await self._cancel_submitted_task(task_id, task)
            if mark_submit_failed:
                await self._mark_submit_failed(task_id, traceback.format_exc())
            raise
        return task_id

    async def _submit_indexing_task(
        self,
        task_id: str,
        submit_kwargs: dict[str, Any],
        *,
        allow_legacy_retry: bool,
    ) -> Any:
        try:
            return await self._submit_indexing_task_once(task_id, submit_kwargs)
        except Exception as exc:
            if not allow_legacy_retry or not _is_legacy_require_existing_partition_rejection(exc):
                raise
            logger.warning(
                "Indexer actor rejected require_existing_partition; retrying without it for rolling-deploy compatibility",
                task_id=task_id,
            )
            legacy_kwargs = dict(submit_kwargs)
            legacy_kwargs.pop(_REQUIRE_EXISTING_PARTITION_KWARG, None)
            return await self._submit_indexing_task_once(task_id, legacy_kwargs)

    async def _submit_indexing_task_once(self, task_id: str, submit_kwargs: dict[str, Any]) -> Any:
        # ``IndexerPool`` is a Ray actor; ``submit`` returns ``[worker_ref]``
        # (wrapped so Ray doesn't auto-dereference and block on the worker task).
        # Awaiting the submit call yields that list; element 0 is the worker ref
        # that ``cancel_task``/``ray.cancel`` must target.
        submitted = await self._call(
            self._pool.submit.remote(**submit_kwargs),
            task_description=f"submit({task_id})",
        )
        return submitted[0]

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

    async def _cancel_submitted_task(self, task_id: str, task: Any) -> bool:
        import ray

        try:
            ray.cancel(task, recursive=True)
        except Exception as exc:
            logger.warning(
                "Failed to cancel submitted indexing task after dispatch failure",
                task_id=task_id,
                error=str(exc),
            )
            raise RuntimeError(f"Failed to cancel submitted indexing task {task_id}") from exc
        try:
            await call_ray_actor_with_timeout(
                future=task,
                timeout=self._timeout,
                task_description=f"cancel_submitted_task({task_id})",
            )
        except TaskCancelledError:
            return True
        except TimeoutError as exc:
            logger.warning(
                "Timed out waiting for submitted indexing task to settle after dispatch failure",
                task_id=task_id,
            )
            raise TimeoutError(
                f"Timed out waiting for submitted indexing task {task_id} to settle after dispatch failure"
            ) from exc
        except Exception as exc:
            logger.info(
                "Submitted indexing task settled after dispatch failure cancellation request",
                task_id=task_id,
                error=str(exc),
            )
            return False
        logger.info(
            "Submitted indexing task completed before dispatch failure cancellation took effect",
            task_id=task_id,
        )
        return False

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

        public_metadata = strip_internal_metadata(metadata)
        entities = []
        for row in rows:
            internal_metadata = {k: v for k, v in row.items() if is_internal_metadata_key(k)}
            entity = dict(row)
            entity.update(public_metadata)
            entity.update(internal_metadata)
            entities.append(entity)

        await self._upsert_entities(entities)

        file_metadata = self._file_metadata_from_chunk(rows[0])
        file_metadata.update(public_metadata)
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

        public_metadata = strip_internal_metadata(metadata)
        entities = []
        for row in rows:
            entity = strip_internal_metadata(row)
            entity.pop("_id", None)
            entity.update(public_metadata)
            entities.append(entity)

        await self._insert_entities(entities)

        target_file_id = metadata.get("file_id", file_id)
        target_partition = metadata.get("partition", partition)
        file_metadata = self._file_metadata_from_chunk(rows[0])
        file_metadata.update(public_metadata)
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
            if k not in self._FILE_METADATA_EXCLUDED_KEYS and not is_internal_metadata_key(k)
        }

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

        # Atomically claim the cancellation first: if ray.cancel() ran before
        # this and the RPC then failed, a killed worker could never report
        # back and the task would be stuck active forever (a zombie).
        # TaskStateManager keeps CANCELLED sticky, so a worker that starts in
        # this small window cannot report active/success after the cancel claim.
        cancelled = await self._call(
            self._tsm.set_cancelled_if_active.remote(task_id),
            task_description=f"set_cancelled_if_active({task_id})",
        )
        if not cancelled:
            return False

        ray.cancel(obj_ref["ref"], recursive=True)
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


def _is_legacy_require_existing_partition_rejection(exc: BaseException) -> bool:
    for current in _exception_chain(exc):
        message = f"{type(current).__name__}: {current}"
        if _REQUIRE_EXISTING_PARTITION_KWARG in message and "unexpected keyword" in message:
            return True
    return False


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


__all__ = ["WorkerDispatcher", "from_ray_namespace"]
