from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

from core.indexing.dispatcher import IndexingDispatcher
from core.models.catalog import DocumentStatus, IndexationJob
from core.utils.conts import is_internal_metadata_key, strip_internal_metadata
from core.utils.logging import get_logger
from ray.exceptions import TaskCancelledError
from services.workers.ray_utils import call_ray_actor_with_timeout
from services.workers.stages.store import INDEXING_TASK_ID_METADATA_KEY
from services.workers.task_cancellation import cancel_active_indexing_tasks

logger = get_logger()

DEFAULT_TIMEOUT = 60.0
_REQUIRE_EXISTING_PARTITION_KWARG = "require_existing_partition"

# Retention for the durable ``jobs`` table (issue #660). Terminal jobs are swept
# opportunistically from the dispatch path rather than by a background task: a
# sweep is only ever needed *because* jobs are being created, and piggybacking on
# dispatch keeps this out of the app lifecycle (no extra task to own, cancel and
# reason about across API replicas). The interval throttle means a burst of a
# thousand uploads still costs one DELETE.
JOB_RETENTION_SECONDS = 7 * 24 * 3600
JOB_RETENTION_MAX_ROWS = 10_000
JOB_PURGE_INTERVAL_SECONDS = 300.0


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
        job_repo: Any = None,
    ) -> None:
        self._pool = pool
        self._tsm = task_state_manager
        self._vector_store = vector_store
        self._document_repo = document_repo
        self._workspace_repo = workspace_repo
        self._collection = collection
        self._timeout = timeout
        # Optional so the dispatcher still runs against a catalog store without a
        # job repository (and so tests can build one without Postgres). When it
        # is absent, job state degrades to the in-memory actor — the pre-#660
        # behaviour.
        self._job_repo = job_repo
        self._last_job_purge_at: float | None = None

    async def _call(self, future: Any, task_description: str) -> Any:
        from services.workers.ray_utils import call_ray_actor_with_timeout

        return await call_ray_actor_with_timeout(
            future=future,
            timeout=self._timeout,
            task_description=task_description,
        )

    async def _set_queued_details(
        self,
        task_id: str,
        *,
        file_id: str | None,
        partition: str,
        metadata: dict[str, Any],
        user_id: int | None,
    ) -> bool:
        remote = _remote_actor_method(self._tsm, "set_queued_details")
        if remote is not None:
            accepted = await self._call(
                remote(
                    task_id,
                    file_id=file_id,
                    partition=partition,
                    metadata=metadata,
                    user_id=user_id,
                ),
                task_description=f"set_queued_details({task_id})",
            )
            return accepted is not False

        await self._call(
            self._tsm.set_state.remote(task_id, "QUEUED"),
            task_description=f"set_state({task_id})",
        )
        await self._call(
            self._tsm.set_details.remote(
                task_id,
                file_id=file_id,
                partition=partition,
                metadata=metadata,
                user_id=user_id,
            ),
            task_description=f"set_details({task_id})",
        )
        return True

    async def _begin_file_delete_fence(self, *, file_id: str, partition: str) -> None:
        remote = _remote_actor_method(self._tsm, "begin_file_delete")
        if remote is None:
            raise RuntimeError("TaskStateManager does not expose file delete fencing for delete cleanup")
        await self._call(
            remote(partition=partition, file_id=file_id),
            task_description=f"begin_file_delete({partition}, {file_id})",
        )

    async def _end_file_delete_fence(self, *, file_id: str, partition: str) -> None:
        remote = _remote_actor_method(self._tsm, "end_file_delete")
        if remote is None:
            raise RuntimeError("TaskStateManager does not expose file delete fencing for delete cleanup")
        await self._call(
            remote(partition=partition, file_id=file_id),
            task_description=f"end_file_delete({partition}, {file_id})",
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
        allow_legacy_require_existing_partition_retry: bool = False,
        quota_reserved: bool = False,
    ) -> str:
        task_id = uuid.uuid4().hex

        user_metadata = {key: value for key, value in metadata.items() if key not in {"file_id", "source"}}
        accepted = await self._set_queued_details(
            task_id,
            file_id=metadata.get("file_id"),
            partition=partition,
            metadata=user_metadata,
            user_id=user.get("id") if user else None,
        )
        if not accepted:
            raise RuntimeError(
                f"Task {task_id} was rejected because file {metadata.get('file_id')!r} "
                f"in partition {partition!r} is being deleted"
            )

        # The durable row is written *before* the task is submitted, so a crash
        # between submit and the worker's first state write still leaves the job
        # visible (as QUEUED) rather than silently in-flight and unobservable.
        #
        # It is written *after* the admission gate above, not before, because
        # #671 made admission refusable: ``_set_queued_details`` returns False
        # when a delete fence covers this file, and forces the in-memory state to
        # CANCELLED. A job that was never admitted has no work to record, so
        # writing QUEUED here would leave a durable row that no worker will ever
        # settle -- non-terminal forever, since retention sweeps terminal rows
        # only, and counted active in ``/queue/info`` for good.
        await self._record_job(
            "create",
            task_id,
            lambda: self._job_repo.create_job(
                IndexationJob(
                    id=task_id,
                    status=DocumentStatus.QUEUED,
                    partition=partition,
                    file_id=metadata.get("file_id"),
                    user_id=user.get("id") if user else None,
                    job_metadata=user_metadata,
                )
            ),
        )
        await self._maybe_purge_jobs()

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
                # #664: tells the worker it owns the reserved file slot and must
                # release it if the file never reaches the catalog.
                "quota_reserved": quota_reserved,
            }
            if require_existing_partition:
                submit_kwargs[_REQUIRE_EXISTING_PARTITION_KWARG] = True
            task = await self._submit_indexing_task(
                task_id,
                submit_kwargs,
                allow_legacy_retry=allow_legacy_require_existing_partition_retry,
            )

            # #671 made this call fail closed: it returns False when a delete
            # fence covers the file, and the handler below then cancels the
            # worker we just started and sweeps its vectors.
            #
            # This is where 4ec0a634 ("don't report a dispatch failure once the
            # worker has started") used to swallow the failure, on the grounds
            # that the worker owns the reserved slot from submit onwards and
            # would run to completion regardless. #671 invalidated that premise:
            # the worker no longer runs to completion, it is rolled back, so the
            # error is truthful and must propagate. The residual cost is a
            # double release (the cancelled worker's finally and the request
            # teardown both give the slot back), which under-counts rather than
            # leaks -- the direction this branch already chose deliberately, and
            # which #700 closes.
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
                    await self._cleanup_submitted_vectors(task_id, metadata=metadata, partition=partition)
            if mark_submit_failed:
                await self._mark_submit_failed(task_id, traceback.format_exc())
            raise
        return task_id

    async def _cleanup_submitted_vectors(self, task_id: str, *, metadata: dict, partition: str) -> None:
        file_id = metadata.get("file_id")
        if not file_id:
            return
        try:
            if await self._vector_store.collection_exists(self._collection):
                await self._vector_store.delete_by_filter(
                    {
                        "partition": partition,
                        "file_id": file_id,
                        INDEXING_TASK_ID_METADATA_KEY: str(task_id),
                    }
                )
        except Exception as exc:
            logger.warning(
                "Failed to clean task-marked vectors after indexing dispatch failure",
                task_id=task_id,
                file_id=file_id,
                partition=partition,
                error=str(exc),
            )

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
        #
        # A timeout here is ambiguous about ownership of the reserved slot: the
        # submit may have started the worker (which then owns it) or not (in
        # which case the request's teardown must release it), and the caller
        # cannot tell which. The wrapper cancels the future and raises, so the
        # router skips ``commit_quota_reservation`` and teardown releases --
        # correct for the overwhelmingly common case that submit never ran, and
        # off by one the other way. Both directions undercount rather than
        # leak, which is the side this branch errs on, and both self-heal under
        # the #676 recount.
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

    async def _record_job(self, action: str, task_id: str, call: Any) -> Any:
        """Run a durable job write, degrading to a warning on failure.

        Postgres is the source of truth for job state, but it is not on the
        critical path of *indexing*: failing an upload because the audit row
        could not be written would turn a monitoring outage into a data-ingest
        outage. The in-memory actor still has the state, so we log loudly and
        continue.
        """
        if self._job_repo is None:
            return None
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 - durable bookkeeping must not fail indexing
            logger.warning(
                "Durable job state write failed; job history for this task may be incomplete",
                action=action,
                task_id=task_id,
                error=str(exc),
            )
            return None

    async def _maybe_purge_jobs(self) -> None:
        """Sweep terminal jobs at most once per ``JOB_PURGE_INTERVAL_SECONDS``."""
        if self._job_repo is None:
            return
        now = time.monotonic()
        if self._last_job_purge_at is not None and now - self._last_job_purge_at < JOB_PURGE_INTERVAL_SECONDS:
            return
        # Stamped before the call so a slow or failing purge cannot be retried on
        # every single dispatch.
        self._last_job_purge_at = now
        purged = await self._record_job(
            "purge",
            "-",
            lambda: self._job_repo.purge_terminal_jobs(
                older_than_seconds=JOB_RETENTION_SECONDS,
                keep_last=JOB_RETENTION_MAX_ROWS,
            ),
        )
        if purged:
            logger.info("Purged terminal indexation jobs past retention", purged=purged)

    async def _get_job(self, task_id: str) -> Any:
        return await self._record_job("get", task_id, lambda: self._job_repo.get_job(task_id))

    async def delete_file(self, file_id: str, partition: str) -> None:
        await self._begin_file_delete_fence(file_id=file_id, partition=partition)
        delete_failed = False
        try:
            await self._delete_file_with_fence(file_id=file_id, partition=partition)
        except Exception:
            delete_failed = True
            raise
        finally:
            try:
                await self._end_file_delete_fence(file_id=file_id, partition=partition)
            except Exception as exc:
                logger.warning(
                    "Failed to release file delete fence",
                    file_id=file_id,
                    partition=partition,
                    error=str(exc),
                )
                if not delete_failed:
                    raise

    async def _delete_file_with_fence(self, *, file_id: str, partition: str) -> None:
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
    ) -> bool:
        """Copy the file's chunks + catalog row; return whether a row was created."""
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
            output_fields=["*", "vector"],
        )
        if not rows:
            return False

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
        return bool(
            await self._document_repo.add_file_to_partition(
                file_id=target_file_id,
                partition=target_partition,
                file_metadata=file_metadata,
                user_id=user.get("id") if user else None,
                relationship_id=file_metadata.get("relationship_id"),
                parent_id=file_metadata.get("parent_id"),
            )
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
        """Read a task's state, hot cache first, Postgres second.

        A miss is not "unknown task": the actor evicts settled tasks and loses
        everything on restart, so the durable row is what makes a task's outcome
        observable afterwards.
        """
        state = await self._call(
            self._tsm.get_state.remote(task_id),
            task_description=f"get_state({task_id})",
        )
        if state is not None:
            return state
        job = await self._get_job(task_id)
        return job.status.value if job else None

    async def get_task_error(self, task_id: str) -> str | None:
        error = await self._call(
            self._tsm.get_error.remote(task_id),
            task_description=f"get_error({task_id})",
        )
        if error is not None:
            return error
        job = await self._get_job(task_id)
        return job.error if job else None

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
            # Already terminal (or evicted): the task reached its own outcome
            # first. Returning before the durable write is what keeps a job that
            # actually COMPLETED from being recorded as CANCELLED in `jobs`.
            return False

        # The durable write is part of the claim, so it happens *before*
        # ``ray.cancel`` -- not after. ``ray.cancel`` kills the only other
        # writer of this row, and everything between here and there runs
        # without a successor that could heal a half-applied cancel:
        # ``_record_job`` catches ``Exception``, but a client disconnect or a
        # graceful shutdown raises ``asyncio.CancelledError``, a
        # ``BaseException`` that sails straight through it. Writing after the
        # kill would leave the actor CANCELLED and the row stuck on its last
        # active status forever -- non-terminal, so ``purge_terminal_jobs``
        # never sweeps it, ``count_by_status`` counts it active forever, and
        # the actor-first and durable-first read paths answer differently for
        # the same task id.
        #
        # Ordering it first cannot mis-record a job that escapes the cancel:
        # the actor claim above already succeeded, and ``update_job`` keeps
        # CANCELLED sticky, so a worker that somehow finishes anyway is
        # declined by the same guard in both stores.
        try:
            # ``shield`` so a client disconnect cannot abort the UPDATE in
            # flight: the write runs to completion even though the
            # ``CancelledError`` propagates to us immediately.
            await asyncio.shield(
                self._record_job(
                    "cancel",
                    task_id,
                    lambda: self._job_repo.update_job(
                        task_id,
                        status=DocumentStatus.CANCELLED,
                        completed_at=datetime.now(UTC),
                    ),
                )
            )
        finally:
            # In a ``finally`` so the worker still dies if the durable write
            # raises: the actor has already claimed the cancellation, and
            # leaving the worker running would contradict both records.
            ray.cancel(obj_ref["ref"], recursive=True)
        # The reserved quota slot (#664) is deliberately not released here.
        # A task cancelled mid-flight gives its slot back in
        # ``IndexerWorker.process_file``'s ``finally``; releasing here too
        # would double-release. But a task that ``ray.cancel`` retires
        # *before* that body runs never executes the ``finally``, so its slot
        # leaks -- and the CANCELLED row written just above is terminal, hence
        # indistinguishable from a clean cancel. Recovering it needs the #676
        # reconciliation to *recount* ``file_count`` (completed files + active
        # job rows), not merely sweep orphaned active rows.
        return True


def from_ray_namespace(
    namespace: str = "openrag",
    timeout: float = DEFAULT_TIMEOUT,
    *,
    vector_store: Any,
    document_repo: Any,
    workspace_repo: Any,
    collection: str,
    job_repo: Any = None,
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
        job_repo=job_repo,
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


def _remote_actor_method(actor: Any, name: str) -> Any | None:
    method_names = getattr(actor, "_ray_actor_method_names", None)
    if isinstance(method_names, (frozenset, list, set, tuple)) and name not in method_names:
        return None
    method = getattr(actor, name, None)
    return getattr(method, "remote", None)


__all__ = ["WorkerDispatcher", "from_ray_namespace"]
