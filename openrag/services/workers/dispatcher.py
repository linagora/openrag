from __future__ import annotations

import asyncio
import traceback
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.indexing.dispatcher import IndexingDispatcher
from core.models.catalog import (
    CONTENT_CLAIM_TOKEN_METADATA_KEY,
    COPY_CONTENT_CLAIM_TOKEN_PREFIX,
    INDEXING_CONTENT_CLAIM_TOKEN_PREFIX,
    TASK_CREATED_AT_METADATA_KEY,
    TASK_FINISHED_AT_METADATA_KEY,
    TERMINAL_TASK_STATES,
    DocumentStatus,
    IndexationJob,
    reconcile_task_state,
)
from core.utils.consts import is_internal_metadata_key, strip_internal_metadata
from core.utils.error_summary import extract_task_error_reason, failure_reason_from_exception
from core.utils.exceptions import ConflictError, mark_indexing_worker_may_be_running
from core.utils.logging import get_logger
from core.vector_stores.vector_field import is_vector_field_key
from ray.exceptions import TaskCancelledError
from services.workers.embedder_provenance import embedder_provenance
from services.workers.failure_reporting import submit_task_failure
from services.workers.ray_utils import call_ray_actor_with_timeout, retry_idempotent_ray_actor_method
from services.workers.stages.store import INDEXING_TASK_ID_METADATA_KEY
from services.workers.task_cancellation import cancel_active_indexing_tasks
from services.workers.task_state import QUEUE_REFUSED_CANCELLED, QUEUE_REFUSED_FILE_INDEXING

logger = get_logger()

DEFAULT_TIMEOUT = 60.0
_FILE_DELETE_FENCE_RENEW_INTERVAL_SECONDS = 30.0
_REQUIRE_EXISTING_PARTITION_KWARG = "require_existing_partition"


class WorkerDispatcher(IndexingDispatcher):
    """Dispatcher that routes new indexing jobs through ``IndexerPool``.

    File mutation paths use the storage ports directly so the API no longer
    depends on the legacy ``Indexer`` actor being present.
    """

    def __init__(
        self,
        *,
        pool: Any,
        task_state_manager: Any,
        completion_tracker: Any,
        vector_store: Any,
        document_repo: Any,
        workspace_repo: Any,
        collection: str,
        job_repo: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._pool = pool
        self._tsm = task_state_manager
        self._completion_tracker = completion_tracker
        self._vector_store = vector_store
        self._document_repo = document_repo
        self._workspace_repo = workspace_repo
        self._job_repo = job_repo
        self._collection = collection
        self._timeout = timeout

    async def _call(self, future: Any, task_description: str) -> Any:
        from services.workers.ray_utils import call_ray_actor_with_timeout

        return await call_ray_actor_with_timeout(
            future=future,
            timeout=self._timeout,
            task_description=task_description,
        )

    async def _call_method(self, submit: Any, task_description: str) -> Any:
        """Submit an actor call inside the availability boundary."""
        from services.workers.ray_utils import call_ray_actor_method_with_timeout

        return await call_ray_actor_method_with_timeout(
            submit=submit,
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
        reject_if_file_active: bool = False,
    ) -> dict[str, Any]:
        """Register the task as QUEUED and report whether the actor took it.

        Returns the ``{"accepted", "reason", "existing_task_id"}`` shape of
        ``set_queued_details_v2``. Against an actor left by an earlier release
        this degrades to the boolean method, which has no admission fence — the
        submission is then accepted exactly as it was before this fence existed,
        never refused on a guess.
        """
        remote_v2 = _remote_actor_method(self._tsm, "set_queued_details_v2")
        if remote_v2 is not None:
            outcome = await retry_idempotent_ray_actor_method(
                submit=lambda: remote_v2(
                    task_id,
                    file_id=file_id,
                    partition=partition,
                    metadata=metadata,
                    user_id=user_id,
                    reject_if_file_active=reject_if_file_active,
                ),
                recovery_timeout=self._timeout,
                task_description=f"set_queued_details_v2({task_id})",
            )
            if isinstance(outcome, dict):
                return outcome
            return _queue_admitted(outcome is not False)

        remote = _remote_actor_method(self._tsm, "set_queued_details")
        if remote is not None:
            if reject_if_file_active:
                logger.bind(task_id=task_id, file_id=file_id, partition=partition).warning(
                    "TaskStateManager predates the file admission fence; a duplicate upload cannot be refused"
                )
            accepted = await retry_idempotent_ray_actor_method(
                submit=lambda: remote(
                    task_id,
                    file_id=file_id,
                    partition=partition,
                    metadata=metadata,
                    user_id=user_id,
                ),
                recovery_timeout=self._timeout,
                task_description=f"set_queued_details({task_id})",
            )
            return _queue_admitted(accepted is not False)

        await self._call_method(
            lambda: self._tsm.set_state.remote(task_id, "QUEUED"),
            task_description=f"set_state({task_id})",
        )
        await self._call_method(
            lambda: self._tsm.set_details.remote(
                task_id,
                file_id=file_id,
                partition=partition,
                metadata=metadata,
                user_id=user_id,
            ),
            task_description=f"set_details({task_id})",
        )
        return _queue_admitted(True)

    async def _active_content_claim_tokens(self, partition: str) -> set[str] | None:
        """Return task tokens whose content reservations must be preserved.

        ``None`` disables orphan recovery for an older TaskStateManager that
        cannot provide the active-task lookup.  That fallback keeps rolling
        deployments conservative.
        """
        remote = _remote_actor_method(self._tsm, "get_content_claim_task_ids")
        if remote is None:
            return None
        task_ids = await self._call_method(
            lambda: remote(partition=partition),
            task_description=f"get_active_content_claim_tokens({partition})",
        )
        if not isinstance(task_ids, (list, set, tuple)):
            raise RuntimeError("TaskStateManager returned invalid claim owners for content claim recovery")
        return {f"{INDEXING_CONTENT_CLAIM_TOKEN_PREFIX}{task_id}" for task_id in task_ids}

    async def _recover_inactive_content_claim(self, *, partition: str, content_sha256: str) -> bool:
        get_lease = getattr(self._document_repo, "get_recoverable_content_sha256_claim", None)
        release_lease = getattr(self._document_repo, "release_recoverable_content_sha256_claim", None)
        if not callable(get_lease) or not callable(release_lease):
            return False

        lease = await get_lease(partition=partition, content_sha256=content_sha256)
        if lease is None:
            return False
        active_claim_tokens = await self._active_content_claim_tokens(partition)
        if active_claim_tokens is None or lease.claim_token in active_claim_tokens:
            return False

        # The lease contains the expiry value observed before the fresh owner
        # lookup. A concurrent renewal changes that value, so the repository's
        # conditional delete fails instead of reclaiming a live reservation.
        return bool(await release_lease(lease))

    async def _active_indexing_task_for_file(self, *, partition: str, file_id: str) -> str | None:
        """Return the task indexing ``file_id`` right now, or ``None``.

        ``None`` also covers an older TaskStateManager without the lookup, and a
        lookup that failed: the caller then keeps the label it already had. The
        submission is refused either way, so an unavailable or slow actor must
        not turn that 409 into a 503 or a 500.
        """
        remote = _remote_actor_method(self._tsm, "get_active_indexing_task_for_file")
        if remote is None:
            return None
        try:
            task_id = await self._call_method(
                lambda: remote(partition=partition, file_id=file_id),
                task_description=f"get_active_indexing_task_for_file({partition}, {file_id})",
            )
        except Exception as exc:
            logger.bind(partition=partition, file_id=file_id).warning(
                "Could not look up the task indexing this file; keeping the content conflict",
                error=str(exc),
            )
            return None
        return task_id if isinstance(task_id, str) and task_id else None

    async def _begin_worker_submission(self, task_id: str) -> bool:
        remote = _remote_actor_method(self._tsm, "begin_worker_submission")
        if remote is None:
            # Older task-state actors do not recover claims early, so their
            # existing conservative behavior is safe during a rolling deploy.
            return True
        accepted = await retry_idempotent_ray_actor_method(
            lambda: remote(task_id),
            recovery_timeout=self._timeout,
            task_description=f"begin_worker_submission({task_id})",
        )
        return accepted is True

    async def _begin_file_delete_fence(self, *, file_id: str, partition: str, fence_id: str) -> None:
        remote = _remote_actor_method(self._tsm, "begin_file_delete")
        if remote is None:
            raise RuntimeError("TaskStateManager does not expose file delete fencing for delete cleanup")
        await retry_idempotent_ray_actor_method(
            lambda: remote(partition=partition, file_id=file_id, fence_id=fence_id),
            recovery_timeout=self._timeout,
            task_description=f"begin_file_delete({partition}, {file_id})",
        )

    async def _end_file_delete_fence(self, *, file_id: str, partition: str, fence_id: str) -> None:
        remote = _remote_actor_method(self._tsm, "end_file_delete")
        if remote is None:
            raise RuntimeError("TaskStateManager does not expose file delete fencing for delete cleanup")
        await retry_idempotent_ray_actor_method(
            lambda: remote(partition=partition, file_id=file_id, fence_id=fence_id),
            recovery_timeout=self._timeout,
            task_description=f"end_file_delete({partition}, {file_id})",
        )

    async def _renew_file_delete_fence(self, *, file_id: str, partition: str, fence_id: str) -> None:
        remote = _remote_actor_method(self._tsm, "renew_file_delete")
        if remote is None:
            raise RuntimeError("TaskStateManager does not expose renewable file delete fencing")
        while True:
            await asyncio.sleep(_FILE_DELETE_FENCE_RENEW_INTERVAL_SECONDS)
            renewed = await retry_idempotent_ray_actor_method(
                lambda: remote(partition=partition, file_id=file_id, fence_id=fence_id),
                recovery_timeout=self._timeout,
                task_description=f"renew_file_delete({partition}, {file_id})",
            )
            if renewed is not True:
                raise RuntimeError("File delete fence lease was lost during cleanup")

    async def _delete_with_renewable_fence(self, *, file_id: str, partition: str, fence_id: str) -> None:
        delete_task = asyncio.create_task(self._delete_file_with_fence(file_id=file_id, partition=partition))
        renewal_task = asyncio.create_task(
            self._renew_file_delete_fence(file_id=file_id, partition=partition, fence_id=fence_id)
        )
        try:
            done, _ = await asyncio.wait({delete_task, renewal_task}, return_when=asyncio.FIRST_COMPLETED)
            if renewal_task in done:
                await renewal_task
            await delete_task
        finally:
            for task in (delete_task, renewal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(delete_task, renewal_task, return_exceptions=True)

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
        callback_url: str | None = None,
        callback_token: str | None = None,
        require_existing_partition: bool = False,
        allow_legacy_require_existing_partition_retry: bool = False,
    ) -> str:
        task_id = uuid.uuid4().hex
        file_id = str(metadata.get("file_id") or "")
        content_sha256 = metadata.get("content_sha256")
        content_claim_token = f"{INDEXING_CONTENT_CLAIM_TOKEN_PREFIX}{task_id}"
        claimed_content = False

        if content_sha256:
            claim_kwargs = {
                "file_id": file_id,
                "partition": partition,
                "content_sha256": content_sha256,
                "claim_token": content_claim_token,
                "replace": replace,
            }
            conflicting_file_id = await self._document_repo.claim_content_sha256(**claim_kwargs)
            if conflicting_file_id is not None and await self._recover_inactive_content_claim(
                partition=partition,
                content_sha256=content_sha256,
            ):
                conflicting_file_id = await self._document_repo.claim_content_sha256(**claim_kwargs)
            if conflicting_file_id is not None:
                # A retry of an upload still being indexed sends the same bytes
                # under the same file_id, so it is the first task's own claim
                # that turns it away here, before the admission fence below
                # ever sees it. Name that task rather than pointing the client
                # at a file the catalog does not show yet.
                if conflicting_file_id == file_id:
                    busy_task_id = await self._active_indexing_task_for_file(partition=partition, file_id=file_id)
                    if busy_task_id is not None:
                        raise _indexing_in_progress(file_id, partition, busy_task_id)
                raise ConflictError(
                    f"This document already exists in partition '{partition}'.",
                    code="DOCUMENT_CONTENT_EXISTS",
                    existing_file_id=conflicting_file_id,
                )
            claimed_content = True

        user_metadata = {
            key: value
            for key, value in metadata.items()
            if key not in {"file_id", "source", TASK_CREATED_AT_METADATA_KEY, TASK_FINISHED_AT_METADATA_KEY}
        }
        user_metadata[TASK_CREATED_AT_METADATA_KEY] = _utc_now_iso()
        task_details = {
            "file_id": file_id,
            "partition": partition,
            "metadata": user_metadata,
            "user_id": user.get("id") if user else None,
        }

        async def release_claim() -> None:
            if claimed_content:
                await self._document_repo.release_content_sha256_claim(
                    file_id=file_id,
                    partition=partition,
                    content_sha256=content_sha256,
                    claim_token=content_claim_token,
                )

        try:
            # A first-time upload is fenced against a task already indexing the
            # same file: the second one can only redo the first one's work and
            # then fail on the catalog insert, after a full parse and embed. A
            # replace is not fenced — re-indexing a file that already exists is
            # what it is for.
            admission = await self._set_queued_details(task_id, reject_if_file_active=not replace, **task_details)
        except BaseException:
            await release_claim()
            raise
        if not admission["accepted"]:
            await release_claim()
            busy_task_id = admission.get("existing_task_id")
            reason = admission.get("reason")
            if reason == QUEUE_REFUSED_FILE_INDEXING and busy_task_id:
                raise _indexing_in_progress(file_id, partition, busy_task_id)
            if reason == QUEUE_REFUSED_CANCELLED:
                raise RuntimeError(f"Task {task_id} was cancelled before it queued")
            raise RuntimeError(
                f"Task {task_id} was rejected because file {file_id!r} in partition {partition!r} is being deleted"
            )

        await self._record_job(
            task_id,
            status=DocumentStatus.QUEUED,
            partition=partition,
            file_id=file_id,
            filename=task_details["metadata"].get("filename"),
            user_id=task_details["user_id"],
        )

        task: Any | None = None
        submission_started = False
        try:
            worker_metadata = dict(metadata)
            if claimed_content:
                worker_metadata[CONTENT_CLAIM_TOKEN_METADATA_KEY] = content_claim_token
            submit_kwargs: dict[str, Any] = {
                "task_id": task_id,
                "path": path,
                "metadata": worker_metadata,
                "partition": partition,
                "user": user,
                "workspace_ids": workspace_ids,
                "replace": replace,
                "indexation_config": indexation_config,
                "embedder_name": embedder_name,
                "callback_url": callback_url,
                "callback_token": callback_token,
            }
            if require_existing_partition:
                submit_kwargs[_REQUIRE_EXISTING_PARTITION_KWARG] = True
            if claimed_content:
                still_owned = await self._document_repo.renew_content_sha256_claim(
                    file_id=file_id,
                    partition=partition,
                    content_sha256=content_sha256,
                    claim_token=content_claim_token,
                )
                if not still_owned:
                    raise ConflictError(
                        "The content reservation expired before indexing started. Please retry the upload.",
                        code="DOCUMENT_CONTENT_CLAIM_LOST",
                    )
            if not await self._begin_worker_submission(task_id):
                raise RuntimeError(f"Task {task_id} was rejected before worker submission")
            submission_started = True
            task = await self._submit_indexing_task(
                task_id,
                submit_kwargs,
                allow_legacy_retry=allow_legacy_require_existing_partition_retry,
            )
            self._track_completion(task_id, task)
        except BaseException as exc:
            submission_outcome_unknown = submission_started and task is None
            mark_submit_failed = not submission_outcome_unknown
            try:
                if task is not None:
                    mark_submit_failed = await self._cancel_submitted_task(task_id, task)
                    if mark_submit_failed:
                        await self._cleanup_submitted_vectors(task_id, metadata=metadata, partition=partition)
                if not submission_outcome_unknown:
                    await self._record_finished_at(task_id, task_details)
                    if mark_submit_failed:
                        tb = traceback.format_exc()
                        error_reason = failure_reason_from_exception(exc)
                        await self._mark_submit_failed(task_id, tb, error_reason)
                        # The completion tracker never saw this task, so nothing
                        # else settles its row: it would stay QUEUED until a
                        # restart reconciled it, long after the actor forgot the
                        # failure.
                        await self._record_job(
                            task_id,
                            status=DocumentStatus.FAILED,
                            partition=partition,
                            file_id=file_id,
                            filename=task_details["metadata"].get("filename"),
                            user_id=task_details["user_id"],
                            error=tb,
                            error_reason=error_reason,
                            completed_at=datetime.now(UTC),
                        )
            finally:
                if claimed_content and not submission_outcome_unknown and (task is None or mark_submit_failed):
                    await self._document_repo.release_content_sha256_claim(
                        file_id=file_id,
                        partition=partition,
                        content_sha256=content_sha256,
                        claim_token=content_claim_token,
                    )
            if submission_outcome_unknown:
                # The pool may have launched a worker that has not read the
                # uploaded file yet; tell the caller to keep it on disk.
                mark_indexing_worker_may_be_running(exc)
            raise
        return task_id

    def _track_completion(self, task_id: str, task: Any) -> None:
        self._completion_tracker.track.remote(task_id, {"ref": task})

    async def _record_finished_at(self, task_id: str, task_details: dict[str, Any]) -> None:
        metadata = dict(task_details["metadata"])
        metadata[TASK_FINISHED_AT_METADATA_KEY] = _utc_now_iso()
        try:
            await self._call_method(
                lambda: self._tsm.set_details.remote(task_id, **{**task_details, "metadata": metadata}),
                task_description=f"set_finished_at({task_id})",
            )
        except Exception as exc:
            logger.warning("Failed to record indexing task completion time", task_id=task_id, error=str(exc))

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
        submitted = await self._call(
            self._pool.submit.remote(**submit_kwargs),
            task_description=f"submit({task_id})",
        )
        return submitted[0]

    async def _mark_submit_failed(self, task_id: str, tb: str, error_reason: str) -> None:
        set_failed = getattr(self._tsm, "set_failed_if_not_cancelled", None)
        if set_failed is not None:
            await self._call_method(
                lambda: submit_task_failure(self._tsm, task_id, tb, error_reason),
                task_description=f"set_failed_if_not_cancelled({task_id})",
            )
            return
        await self._call_method(
            lambda: self._tsm.set_state.remote(task_id, "FAILED"),
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
        fence_id = uuid.uuid4().hex
        await self._begin_file_delete_fence(file_id=file_id, partition=partition, fence_id=fence_id)
        delete_failed = False
        try:
            await self._delete_with_renewable_fence(file_id=file_id, partition=partition, fence_id=fence_id)
        except Exception:
            delete_failed = True
            raise
        finally:
            try:
                await self._end_file_delete_fence(file_id=file_id, partition=partition, fence_id=fence_id)
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
        if await self._document_repo.get_file_metadata(file_id, partition) is None:
            return
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
            # The whole row is written back, so "*" must include the vectors.
            output_fields=["*"],
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

        await self._document_repo.update_file_metadata_in_db(file_id, partition, public_metadata)

    async def copy_file(
        self,
        file_id: str,
        metadata: dict,
        partition: str,
        user: dict | None,
        *,
        vector_field: str | None = None,
        embedder: Any = None,
        embedder_reference: str | None = None,
        embedder_fingerprint: Mapping[str, str | None] | None = None,
    ) -> None:
        source_file_metadata = await self._document_repo.get_file_metadata(file_id, partition)
        if source_file_metadata is None:
            return
        target_file_id = metadata.get("file_id", file_id)
        target_partition = metadata.get("partition", partition)
        content_sha256 = metadata.get("content_sha256")
        claimed_content = False
        claim_token = f"{COPY_CONTENT_CLAIM_TOKEN_PREFIX}{uuid.uuid4().hex}"
        if content_sha256:
            conflicting_file_id = await self._document_repo.claim_content_sha256(
                file_id=target_file_id,
                partition=target_partition,
                content_sha256=content_sha256,
                claim_token=claim_token,
            )
            if conflicting_file_id is not None:
                raise ConflictError(
                    f"This document already exists in partition '{target_partition}'.",
                    code="DOCUMENT_CONTENT_EXISTS",
                    existing_file_id=conflicting_file_id,
                )
            claimed_content = True

        try:
            rows = await self._vector_store.query_chunks_by_filter(
                self._collection,
                {"partition": partition, "file_id": file_id},
                output_fields=["*"],
            )
            if not rows:
                return

            public_metadata = strip_internal_metadata(metadata)
            indexed_at = datetime.now(UTC)
            entities = []
            for row in rows:
                entity = strip_internal_metadata(row)
                entity.pop("_id", None)
                entity.update(public_metadata)
                entity["indexed_at"] = indexed_at.isoformat()
                # Marks this copy's chunks, so a refused catalog write removes exactly them.
                entity[INDEXING_TASK_ID_METADATA_KEY] = claim_token
                entities.append(entity)

            # The source's record, embedder included, still describes vectors copied as they are.
            indexation_config = await self._document_repo.get_indexation_config(file_id, partition)
            catalog_kwargs: dict[str, Any] = {}
            if vector_field is not None and await self._move_to_vector_field(entities, vector_field, embedder):
                indexation_config = {
                    **(indexation_config or {}),
                    **self._copy_provenance(entities, vector_field, embedder, embedder_reference),
                }
                if embedder_fingerprint is not None:
                    catalog_kwargs["embedder_fingerprint"] = embedder_fingerprint
            if indexation_config is not None:
                catalog_kwargs["indexation_config"] = indexation_config
            await self._insert_entities(entities)

            file_metadata = dict(source_file_metadata)
            file_metadata.update(public_metadata)
            file_metadata["indexed_at"] = indexed_at.isoformat()
            try:
                await self._document_repo.add_file_to_partition(
                    file_id=target_file_id,
                    partition=target_partition,
                    file_metadata=file_metadata,
                    user_id=user.get("id") if user else None,
                    relationship_id=file_metadata.get("relationship_id"),
                    parent_id=file_metadata.get("parent_id"),
                    content_sha256=content_sha256,
                    indexed_at=indexed_at,
                    chunk_count=len(entities),
                    **catalog_kwargs,
                )
            except Exception:
                await self._cleanup_submitted_vectors(
                    claim_token, metadata={"file_id": target_file_id}, partition=target_partition
                )
                raise
        finally:
            if claimed_content:
                await self._document_repo.release_content_sha256_claim(
                    file_id=target_file_id,
                    partition=target_partition,
                    content_sha256=content_sha256,
                    claim_token=claim_token,
                )

    async def _move_to_vector_field(self, entities: list[dict[str, Any]], vector_field: str, embedder: Any) -> bool:
        """Give copied chunks a vector in ``vector_field`` and in no other field.

        A partition only searches its embedder's field, so a chunk copied from a
        partition on another embedder is re-embedded from its stored text.
        Returns whether any chunk was.
        """
        missing = [entity for entity in entities if entity.get(vector_field) is None]
        if missing:
            vectors = await embedder.embed([entity.get("text") or "" for entity in missing])
            await self._vector_store.ensure_vector_field(vector_field, len(vectors[0]))
            for entity, vector in zip(missing, vectors, strict=True):
                entity[vector_field] = vector
        for entity in entities:
            for key in [key for key in entity if is_vector_field_key(key) and key != vector_field]:
                del entity[key]
        return bool(missing)

    @staticmethod
    def _copy_provenance(
        entities: list[dict[str, Any]], vector_field: str, embedder: Any, embedder_reference: str | None
    ) -> dict[str, Any]:
        """The embedder record of a copy that was re-embedded: the target's, not the source's."""
        provenance = embedder_provenance(embedder, embedder_reference)
        if provenance["embedder_dimension"] is None:
            # An embedder that does not report its width: the vectors it just made do.
            widths = {len(entity[vector_field]) for entity in entities}
            if len(widths) == 1:
                provenance["embedder_dimension"] = widths.pop()
        return provenance

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

    async def _record_job(
        self,
        task_id: str,
        *,
        status: DocumentStatus,
        partition: str,
        file_id: str | None = None,
        filename: str | None = None,
        user_id: int | None = None,
        error: str | None = None,
        error_reason: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """Mirror a task transition to Postgres. History must never fail indexing."""
        if self._job_repo is None:
            return
        try:
            await self._job_repo.upsert_job(
                IndexationJob(
                    id=task_id,
                    status=status,
                    partition=partition,
                    file_id=file_id,
                    filename=filename,
                    user_id=user_id,
                    error=error,
                    error_reason=error_reason,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
        except Exception as exc:
            logger.warning("Failed to record indexing job", task_id=task_id, error=str(exc))

    async def _durable_job(self, task_id: str) -> IndexationJob | None:
        """Read the durable row, or ``None`` if it cannot be read.

        Reads are best-effort for the same reason writes are: this is a
        fallback for tasks the actor has already forgotten, and a status route
        that 500s during a Postgres outage is worse than one that reports what
        the actor still knows.
        """
        if self._job_repo is None:
            return None
        try:
            return await self._job_repo.get_job(task_id)
        except Exception as exc:
            logger.warning("Failed to read durable job", task_id=task_id, error=str(exc))
            return None

    async def get_task_state(self, task_id: str) -> str | None:
        job = await self._durable_job(task_id)
        actor_state = None
        if job is None or job.status not in TERMINAL_TASK_STATES:
            try:
                actor_state = await self._call_method(
                    lambda: self._tsm.get_state.remote(task_id),
                    task_description=f"get_state({task_id})",
                )
            except Exception:
                if job is None:
                    raise
                logger.warning("Failed to read live task state", task_id=task_id)
        return reconcile_task_state(actor_state, job.status.value if job is not None else None)

    async def get_task_error(self, task_id: str) -> str | None:
        job = await self._durable_job(task_id)
        if job is not None and job.status in TERMINAL_TASK_STATES and job.error is not None:
            return job.error
        try:
            error = await self._call_method(
                lambda: self._tsm.get_error.remote(task_id),
                task_description=f"get_error({task_id})",
            )
        except Exception:
            if job is None:
                raise
            logger.warning("Failed to read live task error", task_id=task_id)
            error = None
        return error if error is not None else (job.error if job is not None else None)

    async def get_task_error_reason(self, task_id: str) -> str | None:
        job = await self._durable_job(task_id)
        if job is not None and job.status in TERMINAL_TASK_STATES and job.error_reason is not None:
            return job.error_reason
        method_names = getattr(self._tsm, "_ray_actor_method_names", None)
        supports_reason = isinstance(method_names, (frozenset, list, set, tuple)) and (
            "get_error_reason" in method_names
        )
        if supports_reason:
            try:
                reason = await self._call_method(
                    lambda: self._tsm.get_error_reason.remote(task_id),
                    task_description=f"get_error_reason({task_id})",
                )
            except Exception:
                if job is None:
                    raise
                logger.warning("Failed to read live task error reason", task_id=task_id)
                reason = None
            if reason is not None:
                return reason
        error = await self.get_task_error(task_id)
        return extract_task_error_reason(error) or (job.error_reason if job is not None else None)

    async def cancel_task(self, task_id: str) -> bool:
        import ray

        obj_ref = await self._call_method(
            lambda: self._tsm.get_object_ref.remote(task_id),
            task_description=f"get_object_ref({task_id})",
        )
        if obj_ref is None:
            return False

        # Atomically claim the cancellation first: if ray.cancel() ran before
        # this and the RPC then failed, a killed worker could never report
        # back and the task would be stuck active forever (a zombie).
        # TaskStateManager keeps CANCELLED sticky, so a worker that starts in
        # this small window cannot report active/success after the cancel claim.
        cancelled = await self._call_method(
            lambda: self._tsm.set_cancelled_if_active.remote(task_id),
            task_description=f"set_cancelled_if_active({task_id})",
        )
        if not cancelled:
            state = await self._call_method(
                lambda: self._tsm.get_state.remote(task_id),
                task_description=f"get_state({task_id}) after cancellation claim",
            )
            if state is None:
                job = await self._durable_job(task_id)
                state = reconcile_task_state(None, job.status.value if job is not None else None)
            if state != "CANCELLED":
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
    job_repo: Any = None,
) -> WorkerDispatcher:
    import ray
    from services.workers.indexer_pool import build_indexer_pool

    return WorkerDispatcher(
        pool=build_indexer_pool(namespace=namespace),
        task_state_manager=ray.get_actor("TaskStateManager", namespace=namespace),
        completion_tracker=ray.get_actor("TaskCompletionTracker", namespace=namespace),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection=collection,
        job_repo=job_repo,
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


def _queue_admitted(accepted: bool) -> dict[str, Any]:
    """Wrap a boolean queue registration in the v2 result shape."""
    return {"accepted": accepted, "reason": None, "existing_task_id": None}


def _indexing_in_progress(file_id: str, partition: str, task_id: str) -> ConflictError:
    return ConflictError(
        f"File '{file_id}' is already being indexed in partition '{partition}'.",
        code="DOCUMENT_INDEXING_IN_PROGRESS",
        existing_task_id=task_id,
    )


def _remote_actor_method(actor: Any, name: str) -> Any | None:
    method_names = getattr(actor, "_ray_actor_method_names", None)
    if isinstance(method_names, (frozenset, list, set, tuple)) and name not in method_names:
        return None
    method = getattr(actor, name, None)
    return getattr(method, "remote", None)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["WorkerDispatcher", "from_ray_namespace"]
