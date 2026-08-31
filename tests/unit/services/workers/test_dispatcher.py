from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.utils.exceptions import ServiceUnavailableError
from ray.exceptions import ActorUnavailableError, TaskCancelledError
from services.workers.task_state import PENDING_TASK_DETAILS


def _remote_mock(return_value: Any = None) -> MagicMock:
    method = MagicMock()
    method.remote = AsyncMock(return_value=return_value)
    return method


def _pool_with_ref(ref: object) -> MagicMock:
    pool = MagicMock()
    # IndexerPool is a Ray actor; submit.remote() picks the least-loaded worker
    # and returns its ObjectRef wrapped in a one-element list (the dispatcher
    # awaits the call and takes element 0).
    pool.submit = _remote_mock([ref])
    return pool


def _settled_ref() -> asyncio.Future[None]:
    ref = asyncio.get_running_loop().create_future()
    ref.set_result(None)
    return ref


def _cancelled_ref() -> asyncio.Future[None]:
    ref = asyncio.get_running_loop().create_future()
    ref.set_exception(TaskCancelledError())
    return ref


def _vector_store() -> MagicMock:
    store = MagicMock()
    store.query_ids_by_filter = AsyncMock(return_value=["1", "2"])
    store.query_chunks_by_filter = AsyncMock(
        return_value=[
            {
                "_id": 1,
                "text": "hello",
                "vector": [0.1, 0.2],
                "file_id": "file-1",
                "partition": "tenant-a",
                "page": 1,
                "section_id": 11,
                "title": "old",
            }
        ]
    )
    store.delete = AsyncMock()
    store.delete_by_filter = AsyncMock(return_value=2)
    store.collection_exists = AsyncMock(return_value=True)
    store.upsert_entities = AsyncMock()
    store.insert_entities = AsyncMock()
    return store


def _document_repo() -> MagicMock:
    repo = MagicMock()
    repo.claim_content_sha256 = AsyncMock(return_value=None)
    repo.release_content_sha256_claim = AsyncMock()
    repo.remove_file_from_partition = AsyncMock()
    repo.update_file_metadata_in_db = AsyncMock(return_value=True)
    repo.add_file_to_partition = AsyncMock(return_value=True)
    return repo


def _workspace_repo() -> MagicMock:
    repo = MagicMock()
    repo.remove_file_from_all_workspaces = AsyncMock()
    return repo


def _task_state_manager() -> MagicMock:
    tsm = MagicMock()
    tsm.set_state = _remote_mock()
    tsm.set_failed_if_not_cancelled = _remote_mock()
    tsm.set_cancelled_if_active = _remote_mock(True)
    tsm.finish_cancellation = _remote_mock()
    tsm.set_details = _remote_mock()
    tsm.set_object_ref = _remote_mock()
    tsm.get_state = _remote_mock("SERIALIZING")
    tsm.get_error = _remote_mock("traceback")
    tsm.get_details = _remote_mock(None)
    tsm.get_object_ref = _remote_mock({"ref": object()})
    tsm.get_matching_active_task_refs_v2 = _remote_mock({})
    tsm.get_matching_active_task_refs = _remote_mock({})
    tsm.get_all_info = None
    tsm.set_queued_details = _remote_mock(True)
    tsm.begin_file_delete = _remote_mock()
    tsm.renew_file_delete = _remote_mock(True)
    tsm.end_file_delete = _remote_mock()
    return tsm


def _completion_tracker() -> MagicMock:
    tracker = MagicMock()
    tracker.track.remote.return_value = object()
    return tracker


def test_from_ray_namespace_does_not_require_legacy_indexer_actor() -> None:
    from services.workers.dispatcher import WorkerDispatcher, from_ray_namespace

    tsm = _task_state_manager()
    pool = _pool_with_ref(object())

    def fake_get_actor(name: str, namespace: str):
        assert namespace == "openrag"
        if name == "TaskStateManager":
            return tsm
        if name == "TaskCompletionTracker":
            return _completion_tracker()
        raise AssertionError(f"unexpected eager actor lookup: {name}")

    with (
        patch("ray.get_actor", side_effect=fake_get_actor),
        patch("services.workers.indexer_pool.build_indexer_pool", return_value=pool),
    ):
        dispatcher = from_ray_namespace(
            vector_store=_vector_store(),
            document_repo=_document_repo(),
            workspace_repo=_workspace_repo(),
            collection="default",
        )

    assert isinstance(dispatcher, WorkerDispatcher)


@pytest.mark.asyncio
async def test_job_lookup_maps_actor_submission_failure_to_unavailability() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    tsm.get_state.remote = MagicMock(
        side_effect=ActorUnavailableError("actor is restarting", actor_id=None),
    )
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        timeout=0.01,
    )

    with pytest.raises(ServiceUnavailableError) as caught:
        await dispatcher.get_task_state("task-1")

    assert caught.value.status_code == 503
    assert caught.value.code == "RAY_ACTOR_UNAVAILABLE"


@pytest.mark.asyncio
async def test_dispatch_maps_queued_details_submission_failure_to_unavailability() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    pool = _pool_with_ref(object())
    tsm = _task_state_manager()
    tsm.set_queued_details.remote = MagicMock(
        side_effect=ActorUnavailableError("actor is restarting", actor_id=None),
    )
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        timeout=0.01,
    )

    with pytest.raises(ServiceUnavailableError) as caught:
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "filename": "report.txt"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
        )

    assert caught.value.status_code == 503
    assert caught.value.code == "RAY_ACTOR_UNAVAILABLE"
    pool.submit.remote.assert_not_called()


@pytest.mark.asyncio
async def test_queue_registration_retries_actor_reconstruction() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    tsm.set_queued_details.remote = AsyncMock(
        side_effect=[ActorUnavailableError("actor is restarting", actor_id=None), True]
    )
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        timeout=1,
    )

    assert await dispatcher._set_queued_details(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=42,
    )
    assert tsm.set_queued_details.remote.await_count == 2


@pytest.mark.asyncio
async def test_dispatch_indexing_queues_worker_pool_task_and_records_ref() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with (
        patch("services.workers.dispatcher.uuid") as mock_uuid,
        patch("services.workers.dispatcher._utc_now_iso", return_value="2026-07-20T08:00:00+00:00"),
    ):
        mock_uuid.uuid4.return_value.hex = "task-1"
        task_id = await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={
                "file_id": "file-1",
                "source": "/data/report.txt",
                "filename": "report.txt",
                "_openrag_job_created_at": "forged-created",
                "_openrag_job_finished_at": "forged-finished",
            },
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=["ws-1"],
            replace=True,
            indexation_config={"parsing_strategy": "pymupdf"},
            embedder_name="embed-fast",
            callback_url="https://cozy.example.com/callback",
            callback_token="jwt-token",
            require_existing_partition=True,
        )

    assert task_id == "task-1"
    tsm.set_queued_details.remote.assert_called_once_with(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={
            "filename": "report.txt",
            "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
        },
        user_id=42,
    )
    tsm.set_state.remote.assert_not_called()
    tsm.set_details.remote.assert_not_called()
    pool.submit.remote.assert_called_once_with(
        task_id="task-1",
        path="/data/report.txt",
        metadata={
            "file_id": "file-1",
            "source": "/data/report.txt",
            "filename": "report.txt",
            "_openrag_job_created_at": "forged-created",
            "_openrag_job_finished_at": "forged-finished",
        },
        partition="tenant-a",
        user={"id": 42},
        workspace_ids=["ws-1"],
        replace=True,
        indexation_config={"parsing_strategy": "pymupdf"},
        embedder_name="embed-fast",
        callback_url="https://cozy.example.com/callback",
        callback_token="jwt-token",
        require_existing_partition=True,
    )
    tsm.set_object_ref.remote.assert_called_once_with("task-1", {"ref": ref})


@pytest.mark.asyncio
async def test_dispatch_indexing_registers_completion_with_detached_tracker() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = asyncio.get_running_loop().create_future()
    tsm = _task_state_manager()
    tracker = _completion_tracker()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(ref),
        task_state_manager=tsm,
        completion_tracker=tracker,
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with (
        patch("services.workers.dispatcher.uuid") as mock_uuid,
        patch("services.workers.dispatcher._utc_now_iso", return_value="2026-07-20T08:00:00+00:00"),
    ):
        mock_uuid.uuid4.return_value.hex = "task-1"
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "source": "/data/report.txt", "filename": "report.txt"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
        )
    tracker.track.remote.assert_called_once_with("task-1", {"ref": ref})
    tsm.set_details.remote.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_indexing_rejects_content_already_claimed() -> None:
    from core.utils.exceptions import ConflictError
    from services.workers.dispatcher import WorkerDispatcher

    repo = _document_repo()
    repo.claim_content_sha256.return_value = "existing-file"
    pool = _pool_with_ref(object())
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=_task_state_manager(),
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=repo,
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with pytest.raises(ConflictError) as exc:
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "new-file", "content_sha256": "abc123"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
        )

    assert exc.value.code == "DOCUMENT_CONTENT_EXISTS"
    assert exc.value.extra["existing_file_id"] == "existing-file"
    pool.submit.remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_indexing_passes_attempt_token_to_claim_and_worker() -> None:
    from core.models.catalog import CONTENT_CLAIM_TOKEN_METADATA_KEY
    from services.workers.dispatcher import WorkerDispatcher

    repo = _document_repo()
    pool = _pool_with_ref(object())
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=_task_state_manager(),
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=repo,
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid:
        mock_uuid.uuid4.return_value.hex = "task-1"
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "content_sha256": "abc123"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
        )

    repo.claim_content_sha256.assert_awaited_once_with(
        file_id="file-1",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="task-1",
        replace=False,
    )
    assert pool.submit.remote.await_args.kwargs["metadata"][CONTENT_CLAIM_TOKEN_METADATA_KEY] == "task-1"


@pytest.mark.asyncio
async def test_dispatch_indexing_releases_claim_when_queueing_fails() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    repo = _document_repo()
    tsm = _task_state_manager()
    tsm.set_queued_details.remote.side_effect = RuntimeError("queue unavailable")
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=repo,
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "new-file", "content_sha256": "abc123"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
        )

    repo.release_content_sha256_claim.assert_awaited_once_with(
        file_id="new-file",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token=repo.claim_content_sha256.await_args.kwargs["claim_token"],
    )


@pytest.mark.asyncio
async def test_dispatch_indexing_rejects_task_when_file_delete_fence_is_active() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    pool = _pool_with_ref(object())
    tsm = _task_state_manager()
    tsm.set_queued_details.remote = AsyncMock(return_value=False)
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with (
        patch("services.workers.dispatcher.uuid") as mock_uuid,
        patch("services.workers.dispatcher._utc_now_iso", return_value="2026-07-20T08:00:00+00:00"),
    ):
        mock_uuid.uuid4.return_value.hex = "task-1"
        with pytest.raises(RuntimeError, match="is being deleted"):
            await dispatcher.dispatch_indexing(
                path="/data/report.txt",
                metadata={"file_id": "file-1", "source": "/data/report.txt", "filename": "report.txt"},
                partition="tenant-a",
                user={"id": 42},
                workspace_ids=["ws-1"],
                replace=True,
            )

    pool.submit.remote.assert_not_called()
    tsm.set_object_ref.remote.assert_not_called()
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_indexing_uses_split_queue_registration_for_legacy_task_state_actor() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    tsm._ray_actor_method_names = {"set_state", "set_details", "set_object_ref"}
    del tsm.set_queued_details
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with (
        patch("services.workers.dispatcher.uuid") as mock_uuid,
        patch("services.workers.dispatcher._utc_now_iso", return_value="2026-07-20T08:00:00+00:00"),
    ):
        mock_uuid.uuid4.return_value.hex = "task-1"
        task_id = await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "source": "/data/report.txt", "filename": "report.txt"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=["ws-1"],
            replace=True,
            require_existing_partition=True,
        )

    assert task_id == "task-1"
    tsm.set_state.remote.assert_called_once_with("task-1", "QUEUED")
    tsm.set_details.remote.assert_called_once_with(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={
            "filename": "report.txt",
            "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
        },
        user_id=42,
    )
    tsm.set_object_ref.remote.assert_called_once_with("task-1", {"ref": ref})


@pytest.mark.asyncio
async def test_dispatch_indexing_omits_false_require_existing_partition_for_legacy_actors() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with (
        patch("services.workers.dispatcher.uuid") as mock_uuid,
        patch(
            "services.workers.dispatcher._utc_now_iso",
            side_effect=["2026-07-20T08:00:00+00:00", "2026-07-20T08:00:02+00:00"],
        ),
    ):
        mock_uuid.uuid4.return_value.hex = "task-1"
        task_id = await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "source": "/data/report.txt"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
            require_existing_partition=False,
        )

    assert task_id == "task-1"
    assert "require_existing_partition" not in pool.submit.remote.call_args.kwargs
    tsm.set_object_ref.remote.assert_called_once_with("task-1", {"ref": ref})


@pytest.mark.asyncio
async def test_dispatch_indexing_retries_without_require_existing_partition_for_legacy_actor() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    legacy_error = RuntimeError("submit(task-1) failed")
    legacy_error.__cause__ = TypeError("process_file() got an unexpected keyword argument 'require_existing_partition'")
    pool = MagicMock()
    pool.submit = MagicMock()
    pool.submit.remote = AsyncMock(side_effect=[legacy_error, [ref]])
    tsm = _task_state_manager()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid:
        mock_uuid.uuid4.return_value.hex = "task-1"
        task_id = await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "source": "/data/report.txt"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
            require_existing_partition=True,
            allow_legacy_require_existing_partition_retry=True,
        )

    assert task_id == "task-1"
    first_call, second_call = pool.submit.remote.call_args_list
    assert first_call.kwargs["require_existing_partition"] is True
    assert "require_existing_partition" not in second_call.kwargs
    tsm.set_object_ref.remote.assert_called_once_with("task-1", {"ref": ref})
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_indexing_does_not_drop_required_partition_guard_for_legacy_actor() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    legacy_error = RuntimeError("submit(task-1) failed")
    legacy_error.__cause__ = TypeError("process_file() got an unexpected keyword argument 'require_existing_partition'")
    pool = MagicMock()
    pool.submit = MagicMock()
    pool.submit.remote = AsyncMock(side_effect=legacy_error)
    tsm = _task_state_manager()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid:
        mock_uuid.uuid4.return_value.hex = "task-1"
        with pytest.raises(RuntimeError, match="submit"):
            await dispatcher.dispatch_indexing(
                path="/data/report.txt",
                metadata={"file_id": "file-1", "source": "/data/report.txt"},
                partition="tenant-a",
                user={"id": 42},
                workspace_ids=None,
                replace=False,
                require_existing_partition=True,
            )

    pool.submit.remote.assert_called_once()
    assert pool.submit.remote.call_args.kwargs["require_existing_partition"] is True
    tsm.set_object_ref.remote.assert_not_called()
    tsm.set_failed_if_not_cancelled.remote.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_indexing_marks_task_failed_when_submit_fails() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    pool = MagicMock()
    pool.submit = MagicMock()
    pool.submit.remote = AsyncMock(side_effect=RuntimeError("submit failed"))
    tsm = _task_state_manager()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with (
        patch("services.workers.dispatcher.uuid") as mock_uuid,
        patch(
            "services.workers.dispatcher._utc_now_iso",
            side_effect=["2026-07-20T08:00:00+00:00", "2026-07-20T08:00:02+00:00"],
        ),
    ):
        mock_uuid.uuid4.return_value.hex = "task-1"
        with pytest.raises(RuntimeError, match="submit failed"):
            await dispatcher.dispatch_indexing(
                path="/data/report.txt",
                metadata={"file_id": "file-1", "source": "/data/report.txt"},
                partition="tenant-a",
                user={"id": 42},
                workspace_ids=None,
                replace=False,
            )

    tsm.set_queued_details.remote.assert_called_once()
    tsm.set_state.remote.assert_not_called()
    tsm.set_details.remote.assert_awaited_once_with(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={
            "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
            "_openrag_job_finished_at": "2026-07-20T08:00:02+00:00",
        },
        user_id=42,
    )
    tsm.set_failed_if_not_cancelled.remote.assert_called_once()
    assert tsm.set_failed_if_not_cancelled.remote.call_args.args[0] == "task-1"
    assert "submit failed" in tsm.set_failed_if_not_cancelled.remote.call_args.args[1]
    tsm.set_object_ref.remote.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_indexing_cancels_worker_when_ref_registration_fails() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _cancelled_ref()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    tsm.set_object_ref.remote = AsyncMock(side_effect=RuntimeError("ref registration failed"))
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid, patch("ray.cancel") as cancel:
        mock_uuid.uuid4.return_value.hex = "task-1"
        with pytest.raises(RuntimeError, match="ref registration failed"):
            await dispatcher.dispatch_indexing(
                path="/data/report.txt",
                metadata={"file_id": "file-1", "source": "/data/report.txt"},
                partition="tenant-a",
                user={"id": 42},
                workspace_ids=None,
                replace=False,
            )

    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_failed_if_not_cancelled.remote.assert_called_once()
    assert tsm.set_failed_if_not_cancelled.remote.call_args.args[0] == "task-1"
    assert "ref registration failed" in tsm.set_failed_if_not_cancelled.remote.call_args.args[1]
    vector_store.delete_by_filter.assert_awaited_once_with(
        {
            "partition": "tenant-a",
            "file_id": "file-1",
            "_openrag_indexing_task_id": "task-1",
        }
    )


@pytest.mark.asyncio
async def test_dispatch_indexing_does_not_mark_failed_when_submitted_worker_already_settled() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    tsm.set_object_ref.remote = AsyncMock(side_effect=RuntimeError("ref registration failed"))
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid, patch("ray.cancel") as cancel:
        mock_uuid.uuid4.return_value.hex = "task-1"
        with pytest.raises(RuntimeError, match="ref registration failed"):
            await dispatcher.dispatch_indexing(
                path="/data/report.txt",
                metadata={"file_id": "file-1", "source": "/data/report.txt"},
                partition="tenant-a",
                user={"id": 42},
                workspace_ids=None,
                replace=False,
            )

    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()
    vector_store.delete_by_filter.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_indexing_does_not_mark_failed_when_submitted_worker_cancel_fails() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    tsm.set_object_ref.remote = AsyncMock(return_value=False)
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with (
        patch("services.workers.dispatcher.uuid") as mock_uuid,
        patch("ray.cancel", side_effect=RuntimeError("cancel failed")),
    ):
        mock_uuid.uuid4.return_value.hex = "task-1"
        with pytest.raises(RuntimeError, match="Failed to cancel submitted indexing task"):
            await dispatcher.dispatch_indexing(
                path="/data/report.txt",
                metadata={"file_id": "file-1", "source": "/data/report.txt"},
                partition="tenant-a",
                user={"id": 42},
                workspace_ids=None,
                replace=False,
            )

    tsm.set_failed_if_not_cancelled.remote.assert_not_called()
    vector_store.delete_by_filter.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_indexing_does_not_mark_failed_when_submitted_worker_cancel_times_out() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = asyncio.get_running_loop().create_future()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    tsm.set_object_ref.remote = AsyncMock(return_value=False)
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        timeout=0.01,
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid, patch("ray.cancel"):
        mock_uuid.uuid4.return_value.hex = "task-1"
        with pytest.raises(TimeoutError, match="submitted indexing task task-1"):
            await dispatcher.dispatch_indexing(
                path="/data/report.txt",
                metadata={"file_id": "file-1", "source": "/data/report.txt"},
                partition="tenant-a",
                user={"id": 42},
                workspace_ids=None,
                replace=False,
            )

    tsm.set_failed_if_not_cancelled.remote.assert_not_called()
    vector_store.delete_by_filter.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_indexing_keeps_fast_finished_task_when_ref_registration_is_settled() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    tsm.set_object_ref.remote = AsyncMock(return_value=True)
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid, patch("ray.cancel") as cancel:
        mock_uuid.uuid4.return_value.hex = "task-1"
        task_id = await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "source": "/data/report.txt"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
        )

    assert task_id == "task-1"
    cancel.assert_not_called()
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_indexing_cancels_worker_when_ref_registration_is_rejected() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _cancelled_ref()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    tsm.set_object_ref.remote = AsyncMock(return_value=False)
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid, patch("ray.cancel") as cancel:
        mock_uuid.uuid4.return_value.hex = "task-1"
        with pytest.raises(RuntimeError, match="cancelled before worker ref registration"):
            await dispatcher.dispatch_indexing(
                path="/data/report.txt",
                metadata={"file_id": "file-1", "source": "/data/report.txt"},
                partition="tenant-a",
                user={"id": 42},
                workspace_ids=None,
                replace=False,
            )

    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_failed_if_not_cancelled.remote.assert_called_once()
    vector_store.delete_by_filter.assert_awaited_once_with(
        {
            "partition": "tenant-a",
            "file_id": "file-1",
            "_openrag_indexing_task_id": "task-1",
        }
    )


@pytest.mark.asyncio
async def test_worker_dispatcher_mutates_files_without_legacy_indexer() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    vector_store = _vector_store()
    vector_store.query_chunks_by_filter.return_value[0]["_openrag_indexing_task_id"] = "task-1"
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=_task_state_manager(),
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    await dispatcher.delete_file("file-1", "tenant-a")
    await dispatcher.update_file_metadata(
        "file-1",
        {"title": "new", "_openrag_indexing_task_id": "from-user-metadata"},
        "tenant-a",
        user={"id": 7},
    )
    await dispatcher.copy_file("file-1", {"file_id": "copy-1", "partition": "tenant-b"}, "tenant-b", user=None)

    assert [call.args for call in vector_store.delete_by_filter.call_args_list] == [
        ({"partition": "tenant-a", "file_id": "file-1"},),
        ({"partition": "tenant-a", "file_id": "file-1"},),
    ]
    vector_store.delete.assert_not_called()
    workspace_repo.remove_file_from_all_workspaces.assert_called_once_with("file-1", "tenant-a")
    document_repo.remove_file_from_partition.assert_called_once_with(file_id="file-1", partition="tenant-a")
    document_repo.update_file_metadata_in_db.assert_called_once_with(
        "file-1",
        "tenant-a",
        {"file_id": "file-1", "partition": "tenant-a", "title": "new"},
    )
    document_repo.add_file_to_partition.assert_called_once_with(
        file_id="copy-1",
        partition="tenant-b",
        file_metadata={"file_id": "copy-1", "partition": "tenant-b", "title": "old"},
        user_id=None,
        relationship_id=None,
        parent_id=None,
        content_sha256=None,
    )
    vector_store.upsert_entities.assert_awaited_once()
    vector_store.insert_entities.assert_awaited_once()
    assert vector_store.upsert_entities.await_args.args[0][0]["_openrag_indexing_task_id"] == "task-1"
    assert "_openrag_indexing_task_id" not in vector_store.insert_entities.await_args.args[0][0]


@pytest.mark.asyncio
async def test_cancel_task_uses_stored_pool_object_ref() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    tsm = _task_state_manager()
    tsm.get_object_ref.remote = AsyncMock(return_value={"ref": ref})
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel") as cancel:
        result = await dispatcher.cancel_task("task-1")

    assert result is True
    tsm.set_cancelled_if_active.remote.assert_called_once_with("task-1")
    tsm.finish_cancellation.remote.assert_called_once_with("task-1")
    cancel.assert_called_once_with(ref, recursive=True)


@pytest.mark.asyncio
async def test_cancel_task_leaves_content_claim_for_task_settlement() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    repo = _document_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=repo,
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel"):
        assert await dispatcher.cancel_task("task-1") is True

    repo.release_content_sha256_claim.assert_not_awaited()
    tsm.get_details.remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_task_does_not_cancel_terminal_task() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    tsm = _task_state_manager()
    tsm.get_object_ref.remote = AsyncMock(return_value={"ref": ref})
    tsm.set_cancelled_if_active.remote = AsyncMock(return_value=False)
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel") as cancel:
        result = await dispatcher.cancel_task("task-1")

    assert result is False
    tsm.set_cancelled_if_active.remote.assert_called_once_with("task-1")
    cancel.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_task_finishes_recovered_cancellation_claim() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    tsm = _task_state_manager()
    tsm.get_object_ref.remote = AsyncMock(return_value={"ref": ref})
    tsm.set_cancelled_if_active.remote = AsyncMock(return_value=False)
    tsm.get_state.remote = AsyncMock(return_value="CANCELLED")
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel") as cancel:
        assert await dispatcher.cancel_task("task-1") is True

    cancel.assert_called_once_with(ref, recursive=True)
    tsm.finish_cancellation.remote.assert_called_once_with("task-1")


@pytest.mark.asyncio
async def test_delete_file_cleans_vector_store_before_database() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()

    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=_task_state_manager(),
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    call_order = []
    workspace_repo.remove_file_from_all_workspaces = AsyncMock(
        side_effect=lambda *a, **k: call_order.append("workspace")
    )
    document_repo.remove_file_from_partition = AsyncMock(side_effect=lambda *a, **k: call_order.append("document"))
    vector_store.delete_by_filter = AsyncMock(side_effect=lambda *a, **k: call_order.append("delete_by_filter") or 2)

    await dispatcher.delete_file("file-1", "tenant-a")

    assert call_order == ["delete_by_filter", "workspace", "document", "delete_by_filter"]


@pytest.mark.asyncio
async def test_delete_file_holds_file_delete_fence_around_cleanup() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    call_order = []
    tsm = _task_state_manager()
    tsm.begin_file_delete.remote = AsyncMock(side_effect=lambda **kwargs: call_order.append("begin"))
    tsm.get_matching_active_task_refs_v2.remote = AsyncMock(
        side_effect=lambda **kwargs: call_order.append("lookup") or {}
    )
    tsm.end_file_delete.remote = AsyncMock(side_effect=lambda **kwargs: call_order.append("end"))
    vector_store = _vector_store()
    vector_store.collection_exists = AsyncMock(side_effect=lambda collection: call_order.append("exists") or True)
    vector_store.delete_by_filter = AsyncMock(side_effect=lambda *args, **kwargs: call_order.append("delete") or 2)
    workspace_repo = _workspace_repo()
    workspace_repo.remove_file_from_all_workspaces = AsyncMock(
        side_effect=lambda *args, **kwargs: call_order.append("workspace")
    )
    document_repo = _document_repo()
    document_repo.remove_file_from_partition = AsyncMock(
        side_effect=lambda *args, **kwargs: call_order.append("document")
    )
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    await dispatcher.delete_file("file-1", "tenant-a")

    assert call_order == ["begin", "lookup", "exists", "delete", "workspace", "document", "delete", "end"]
    begin_kwargs = tsm.begin_file_delete.remote.await_args.kwargs
    end_kwargs = tsm.end_file_delete.remote.await_args.kwargs
    assert begin_kwargs["partition"] == "tenant-a"
    assert begin_kwargs["file_id"] == "file-1"
    assert end_kwargs == begin_kwargs


@pytest.mark.asyncio
async def test_delete_file_renews_fence_during_slow_cleanup(monkeypatch) -> None:
    from services.workers import dispatcher as dispatcher_module
    from services.workers.dispatcher import WorkerDispatcher

    renewed = asyncio.Event()
    tsm = _task_state_manager()
    tsm.renew_file_delete.remote = AsyncMock(side_effect=lambda **kwargs: renewed.set() or True)
    vector_store = _vector_store()

    async def wait_for_renewal(_collection: str) -> bool:
        await renewed.wait()
        return False

    vector_store.collection_exists = AsyncMock(side_effect=wait_for_renewal)
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )
    monkeypatch.setattr(dispatcher_module, "_FILE_DELETE_FENCE_RENEW_INTERVAL_SECONDS", 0)

    await dispatcher.delete_file("file-1", "tenant-a")

    tsm.renew_file_delete.remote.assert_awaited()


@pytest.mark.asyncio
async def test_delete_file_stops_cleanup_if_fence_renewal_is_lost(monkeypatch) -> None:
    from services.workers import dispatcher as dispatcher_module
    from services.workers.dispatcher import WorkerDispatcher

    cleanup_cancelled = asyncio.Event()
    tsm = _task_state_manager()
    tsm.renew_file_delete.remote = AsyncMock(return_value=False)
    vector_store = _vector_store()

    async def block_cleanup(_collection: str) -> bool:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_cancelled.set()

    vector_store.collection_exists = AsyncMock(side_effect=block_cleanup)
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )
    monkeypatch.setattr(dispatcher_module, "_FILE_DELETE_FENCE_RENEW_INTERVAL_SECONDS", 0)

    with pytest.raises(RuntimeError, match="lease was lost"):
        await dispatcher.delete_file("file-1", "tenant-a")

    assert cleanup_cancelled.is_set()
    tsm.end_file_delete.remote.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_file_releases_file_delete_fence_when_cleanup_fails() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    vector_store = _vector_store()
    vector_store.delete_by_filter = AsyncMock(side_effect=Exception("Milvus connection failed"))
    workspace_repo = _workspace_repo()
    document_repo = _document_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    with pytest.raises(Exception, match="Milvus connection failed"):
        await dispatcher.delete_file("file-1", "tenant-a")

    tsm.end_file_delete.remote.assert_awaited_once()
    assert tsm.end_file_delete.remote.await_args.kwargs["partition"] == "tenant-a"
    assert tsm.end_file_delete.remote.await_args.kwargs["file_id"] == "file-1"
    workspace_repo.remove_file_from_all_workspaces.assert_not_called()
    document_repo.remove_file_from_partition.assert_not_called()


@pytest.mark.asyncio
async def test_delete_file_fails_closed_when_file_delete_fence_is_missing() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    del tsm.begin_file_delete
    vector_store = _vector_store()
    workspace_repo = _workspace_repo()
    document_repo = _document_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    with pytest.raises(RuntimeError, match="file delete fencing"):
        await dispatcher.delete_file("file-1", "tenant-a")

    vector_store.delete_by_filter.assert_not_called()
    workspace_repo.remove_file_from_all_workspaces.assert_not_called()
    document_repo.remove_file_from_partition.assert_not_called()


@pytest.mark.asyncio
async def test_delete_file_cancels_active_matching_indexing_task_before_cleanup() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2 = _remote_mock({"task-1": {"ref": ref}})
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    tsm.get_matching_active_task_refs_v2.remote.assert_called_once_with(partition="tenant-a", file_id="file-1")
    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_state.remote.assert_any_call("task-1", "CANCELLED")


@pytest.mark.asyncio
async def test_delete_file_waits_for_matching_task_ref_before_cleanup() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2.remote = AsyncMock(
        side_effect=[
            {"task-1": {"ref": None}},
            {"task-1": {"ref": ref}},
        ]
    )
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.task_cancellation._REF_WAIT_INTERVAL", 0), patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    assert tsm.get_matching_active_task_refs_v2.remote.call_count == 2
    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_state.remote.assert_any_call("task-1", "CANCELLED")
    assert vector_store.delete_by_filter.await_count == 2


@pytest.mark.asyncio
async def test_delete_file_rechecks_ref_less_task_before_marking_stale() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2.remote = AsyncMock(
        side_effect=[
            {"task-1": {"ref": None}},
            {"task-1": {"ref": ref}},
        ]
    )
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        timeout=0.01,
    )

    with patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    assert tsm.get_matching_active_task_refs_v2.remote.call_count == 2
    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()
    tsm.set_state.remote.assert_any_call("task-1", "CANCELLED")
    assert vector_store.delete_by_filter.await_count == 2


@pytest.mark.asyncio
async def test_delete_file_rechecks_pending_task_details_before_cleanup() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2.remote = AsyncMock(
        side_effect=[
            {"task-1": PENDING_TASK_DETAILS},
            {},
        ]
    )
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.task_cancellation._REF_WAIT_INTERVAL", 0), patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    assert tsm.get_matching_active_task_refs_v2.remote.call_count == 2
    cancel.assert_not_called()
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()
    tsm.set_state.remote.assert_not_called()
    assert vector_store.delete_by_filter.await_count == 2


@pytest.mark.asyncio
async def test_delete_file_fails_closed_when_pending_task_details_do_not_settle() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2.remote = AsyncMock(return_value={"task-1": PENDING_TASK_DETAILS})
    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
        timeout=0.01,
    )

    with pytest.raises(TimeoutError, match="routing details"), patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    cancel.assert_not_called()
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()
    tsm.set_state.remote.assert_not_called()
    vector_store.delete_by_filter.assert_not_called()
    workspace_repo.remove_file_from_all_workspaces.assert_not_called()
    document_repo.remove_file_from_partition.assert_not_called()


@pytest.mark.asyncio
async def test_delete_file_final_ref_recheck_stays_within_delete_timeout() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2.remote = AsyncMock(return_value={"task-1": {"ref": None}})
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        timeout=0.5,
    )
    timeouts: list[tuple[str, float]] = []

    async def bounded_call(*, submit: Any, timeout: float, task_description: str) -> Any:
        timeouts.append((task_description, timeout))
        return await submit()

    with (
        patch("services.workers.task_cancellation._REF_WAIT_INTERVAL", 999),
        patch("services.workers.task_cancellation.call_ray_actor_method_with_timeout", side_effect=bounded_call),
    ):
        await dispatcher.delete_file("file-1", "tenant-a")

    assert all(0 < timeout <= 0.5 for _, timeout in timeouts)
    assert any("final" in description for description, _ in timeouts)
    tsm.set_failed_if_not_cancelled.remote.assert_called_once()
    assert vector_store.delete_by_filter.await_count == 2


@pytest.mark.asyncio
async def test_delete_file_waits_for_cancelled_task_to_settle_before_cleanup() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    settled = asyncio.Event()
    ref = asyncio.create_task(settled.wait())
    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2 = _remote_mock({"task-1": {"ref": ref}})
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel") as cancel:
        delete_task = asyncio.create_task(dispatcher.delete_file("file-1", "tenant-a"))
        await asyncio.sleep(0)
        assert vector_store.delete_by_filter.await_count == 0
        settled.set()
        await delete_task

    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_state.remote.assert_any_call("task-1", "CANCELLED")
    assert vector_store.delete_by_filter.await_count == 2


@pytest.mark.asyncio
async def test_delete_file_fails_closed_when_active_task_lookup_missing() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    del tsm.get_matching_active_task_refs_v2
    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    with pytest.raises(RuntimeError, match="active-task lookup"):
        await dispatcher.delete_file("file-1", "tenant-a")

    vector_store.delete_by_filter.assert_not_called()
    workspace_repo.remove_file_from_all_workspaces.assert_not_called()
    document_repo.remove_file_from_partition.assert_not_called()


@pytest.mark.asyncio
async def test_delete_file_uses_legacy_task_state_lookup_when_matching_api_missing() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    tsm = _task_state_manager()
    del tsm.get_matching_active_task_refs_v2
    tsm.get_all_info = _remote_mock(
        {
            "task-1": {
                "state": "SERIALIZING",
                "details": {"partition": "tenant-a", "file_id": "file-1"},
            },
            "other-partition": {
                "state": "SERIALIZING",
                "details": {"partition": "tenant-b", "file_id": "file-1"},
            },
            "completed": {
                "state": "COMPLETED",
                "details": {"partition": "tenant-a", "file_id": "file-1"},
            },
        }
    )
    tsm.get_object_ref = _remote_mock({"ref": ref})
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    tsm.get_all_info.remote.assert_called_once_with()
    tsm.get_object_ref.remote.assert_called_once_with("task-1")
    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_state.remote.assert_any_call("task-1", "CANCELLED")
    assert vector_store.delete_by_filter.await_count == 2


@pytest.mark.asyncio
async def test_delete_file_ignores_unsafe_legacy_matching_api_when_v2_missing() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    tsm = _task_state_manager()
    tsm._ray_actor_method_names = {
        "begin_file_delete",
        "end_file_delete",
        "renew_file_delete",
        "get_matching_active_task_refs",
        "get_all_info",
        "get_object_ref",
        "set_state",
    }
    tsm.get_matching_active_task_refs = _remote_mock({"unsafe-task": {"ref": ref}})
    tsm.get_all_info = _remote_mock({})
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    tsm.get_matching_active_task_refs.remote.assert_not_called()
    tsm.get_all_info.remote.assert_called_once_with()
    cancel.assert_not_called()
    assert vector_store.delete_by_filter.await_count == 2


@pytest.mark.asyncio
async def test_delete_file_legacy_lookup_blocks_detail_less_active_task() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    del tsm.get_matching_active_task_refs_v2
    tsm.get_all_info = _remote_mock(
        {
            "task-1": {
                "state": "QUEUED",
                "details": {},
            },
            "other-partition": {
                "state": "SERIALIZING",
                "details": {"partition": "tenant-b", "file_id": "file-1"},
            },
        }
    )
    vector_store = _vector_store()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        timeout=0.01,
    )

    with pytest.raises(TimeoutError, match="routing details"), patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    assert tsm.get_all_info.remote.call_count == 2
    tsm.get_object_ref.remote.assert_not_called()
    cancel.assert_not_called()
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()
    tsm.set_state.remote.assert_not_called()
    vector_store.delete_by_filter.assert_not_called()


@pytest.mark.asyncio
async def test_delete_file_does_not_cleanup_when_cancelled_task_does_not_settle() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = asyncio.get_running_loop().create_future()
    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2 = _remote_mock({"task-1": {"ref": ref}})
    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
        timeout=0.01,
    )

    with pytest.raises(TimeoutError, match="settle after cancellation request"), patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    assert cancel.call_count >= 1
    tsm.set_state.remote.assert_not_called()
    vector_store.delete_by_filter.assert_not_called()
    workspace_repo.remove_file_from_all_workspaces.assert_not_called()
    document_repo.remove_file_from_partition.assert_not_called()


@pytest.mark.asyncio
async def test_delete_file_marks_stale_ref_less_task_failed_before_cleanup() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2.remote = AsyncMock(return_value={"task-1": {"ref": None}})
    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
        timeout=0.01,
    )

    with patch("ray.cancel") as cancel:
        await dispatcher.delete_file("file-1", "tenant-a")

    cancel.assert_not_called()
    tsm.set_failed_if_not_cancelled.remote.assert_called_once()
    assert tsm.set_failed_if_not_cancelled.remote.call_args.args[0] == "task-1"
    assert vector_store.delete_by_filter.await_count == 2
    workspace_repo.remove_file_from_all_workspaces.assert_called_once_with("file-1", "tenant-a")
    document_repo.remove_file_from_partition.assert_called_once_with(file_id="file-1", partition="tenant-a")


@pytest.mark.asyncio
async def test_delete_file_does_not_cleanup_when_matching_task_cancel_fails() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = _settled_ref()
    tsm = _task_state_manager()
    tsm.get_matching_active_task_refs_v2.remote = AsyncMock(return_value={"task-1": {"ref": ref}})
    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    with pytest.raises(RuntimeError, match="Failed to cancel"), patch("ray.cancel", side_effect=RuntimeError("boom")):
        await dispatcher.delete_file("file-1", "tenant-a")

    tsm.set_state.remote.assert_not_called()
    vector_store.delete_by_filter.assert_not_called()
    workspace_repo.remove_file_from_all_workspaces.assert_not_called()
    document_repo.remove_file_from_partition.assert_not_called()


@pytest.mark.asyncio
async def test_delete_file_does_not_remove_database_row_if_vector_store_delete_fails() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()

    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=_task_state_manager(),
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    vector_store.delete_by_filter = AsyncMock(side_effect=Exception("Milvus connection failed"))

    with pytest.raises(Exception, match="Milvus connection failed"):
        await dispatcher.delete_file("file-1", "tenant-a")

    workspace_repo.remove_file_from_all_workspaces.assert_not_called()
    document_repo.remove_file_from_partition.assert_not_called()


@pytest.mark.asyncio
async def test_delete_file_reports_failure_when_post_delete_cleanup_fails() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()

    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=_task_state_manager(),
        completion_tracker=_completion_tracker(),
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    vector_store.delete_by_filter = AsyncMock(side_effect=[2, Exception("Milvus connection failed")])

    with pytest.raises(Exception, match="Milvus connection failed"):
        await dispatcher.delete_file("file-1", "tenant-a")

    workspace_repo.remove_file_from_all_workspaces.assert_called_once_with("file-1", "tenant-a")
    document_repo.remove_file_from_partition.assert_called_once_with(file_id="file-1", partition="tenant-a")
    assert vector_store.delete_by_filter.await_count == 2
