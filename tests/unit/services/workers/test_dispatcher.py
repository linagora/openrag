from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ray.exceptions import TaskCancelledError
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
    tsm.set_details = _remote_mock()
    tsm.set_object_ref = _remote_mock()
    tsm.get_state = _remote_mock("SERIALIZING")
    tsm.get_error = _remote_mock("traceback")
    tsm.get_object_ref = _remote_mock({"ref": object()})
    tsm.get_matching_active_task_refs_v2 = _remote_mock({})
    tsm.get_matching_active_task_refs = _remote_mock({})
    tsm.get_all_info = None
    tsm.set_queued_details = _remote_mock(True)
    tsm.begin_file_delete = _remote_mock()
    tsm.end_file_delete = _remote_mock()
    return tsm


def test_from_ray_namespace_does_not_require_legacy_indexer_actor() -> None:
    from services.workers.dispatcher import WorkerDispatcher, from_ray_namespace

    tsm = _task_state_manager()
    pool = _pool_with_ref(object())

    def fake_get_actor(name: str, namespace: str):
        assert namespace == "openrag"
        if name == "TaskStateManager":
            return tsm
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
async def test_dispatch_indexing_queues_worker_pool_task_and_records_ref() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    pool = _pool_with_ref(ref)
    tsm = _task_state_manager()
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid:
        mock_uuid.uuid4.return_value.hex = "task-1"
        task_id = await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "source": "/data/report.txt", "filename": "report.txt"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=["ws-1"],
            replace=True,
            indexation_config={"parsing_strategy": "pymupdf"},
            embedder_name="embed-fast",
            require_existing_partition=True,
        )

    assert task_id == "task-1"
    tsm.set_queued_details.remote.assert_called_once_with(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={"filename": "report.txt"},
        user_id=42,
    )
    tsm.set_state.remote.assert_not_called()
    tsm.set_details.remote.assert_not_called()
    pool.submit.remote.assert_called_once_with(
        task_id="task-1",
        path="/data/report.txt",
        metadata={"file_id": "file-1", "source": "/data/report.txt", "filename": "report.txt"},
        partition="tenant-a",
        user={"id": 42},
        workspace_ids=["ws-1"],
        replace=True,
        indexation_config={"parsing_strategy": "pymupdf"},
        embedder_name="embed-fast",
        quota_reserved=False,
        require_existing_partition=True,
    )
    tsm.set_object_ref.remote.assert_called_once_with("task-1", {"ref": ref})


@pytest.mark.asyncio
async def test_dispatch_indexing_rejects_task_when_file_delete_fence_is_active() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    pool = _pool_with_ref(object())
    tsm = _task_state_manager()
    tsm.set_queued_details.remote = AsyncMock(return_value=False)
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid:
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
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid:
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
        metadata={"filename": "report.txt"},
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
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("services.workers.dispatcher.uuid") as mock_uuid:
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
    tsm.set_details.remote.assert_not_called()
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
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel") as cancel:
        result = await dispatcher.cancel_task("task-1")

    assert result is True
    tsm.set_cancelled_if_active.remote.assert_called_once_with("task-1")
    cancel.assert_called_once_with(ref, recursive=True)


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
async def test_delete_file_cleans_vector_store_before_database() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    vector_store = _vector_store()
    document_repo = _document_repo()
    workspace_repo = _workspace_repo()

    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=_task_state_manager(),
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
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    await dispatcher.delete_file("file-1", "tenant-a")

    assert call_order == ["begin", "lookup", "exists", "delete", "workspace", "document", "delete", "end"]
    tsm.begin_file_delete.remote.assert_awaited_once_with(partition="tenant-a", file_id="file-1")
    tsm.end_file_delete.remote.assert_awaited_once_with(partition="tenant-a", file_id="file-1")


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
        vector_store=vector_store,
        document_repo=document_repo,
        workspace_repo=workspace_repo,
        collection="default",
    )

    with pytest.raises(Exception, match="Milvus connection failed"):
        await dispatcher.delete_file("file-1", "tenant-a")

    tsm.end_file_delete.remote.assert_awaited_once_with(partition="tenant-a", file_id="file-1")
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
        vector_store=vector_store,
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        timeout=0.5,
    )
    timeouts: list[tuple[str, float]] = []

    async def bounded_call(*, future: Any, timeout: float, task_description: str) -> Any:
        timeouts.append((task_description, timeout))
        return await future

    with (
        patch("services.workers.task_cancellation._REF_WAIT_INTERVAL", 999),
        patch("services.workers.task_cancellation.call_ray_actor_with_timeout", side_effect=bounded_call),
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


# ---------------------------------------------------------------------------
# Durable job records (issue #660)
# ---------------------------------------------------------------------------


def _job_repo() -> MagicMock:
    repo = MagicMock()
    repo.create_job = AsyncMock(side_effect=lambda job: job)
    repo.update_job = AsyncMock(return_value=None)
    repo.get_job = AsyncMock(return_value=None)
    repo.purge_terminal_jobs = AsyncMock(return_value=0)
    return repo


def _dispatcher_with_job_repo(job_repo: Any, tsm: Any = None, ref: object | None = None) -> Any:
    from services.workers.dispatcher import WorkerDispatcher

    return WorkerDispatcher(
        pool=_pool_with_ref(ref if ref is not None else object()),
        task_state_manager=tsm or _task_state_manager(),
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
        job_repo=job_repo,
    )


@pytest.mark.asyncio
async def test_dispatch_indexing_persists_a_queued_job_before_submitting() -> None:
    from core.models.catalog import DocumentStatus

    job_repo = _job_repo()
    dispatcher = _dispatcher_with_job_repo(job_repo)

    with patch("services.workers.dispatcher.uuid") as mock_uuid:
        mock_uuid.uuid4.return_value.hex = "task-1"
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1", "source": "/data/report.txt", "filename": "report.txt"},
            partition="tenant-a",
            user={"id": 42},
            workspace_ids=None,
            replace=False,
        )

    job = job_repo.create_job.await_args.args[0]
    assert job.id == "task-1"
    assert job.status is DocumentStatus.QUEUED
    assert job.partition == "tenant-a"
    assert job.file_id == "file-1"
    assert job.user_id == 42
    assert job.job_metadata == {"filename": "report.txt"}


@pytest.mark.asyncio
async def test_dispatch_indexing_survives_a_job_repo_outage() -> None:
    job_repo = _job_repo()
    job_repo.create_job = AsyncMock(side_effect=RuntimeError("postgres down"))
    dispatcher = _dispatcher_with_job_repo(job_repo)

    task_id = await dispatcher.dispatch_indexing(
        path="/data/report.txt",
        metadata={"file_id": "file-1"},
        partition="tenant-a",
        user=None,
        workspace_ids=None,
        replace=False,
    )

    assert task_id
    # indexing still went out to the pool despite the durable write failing
    dispatcher._pool.submit.remote.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_task_marks_the_durable_job_cancelled() -> None:
    from core.models.catalog import DocumentStatus

    job_repo = _job_repo()
    dispatcher = _dispatcher_with_job_repo(job_repo)

    with patch("ray.cancel"):
        assert await dispatcher.cancel_task("task-1") is True

    job_repo.update_job.assert_awaited_once()
    assert job_repo.update_job.await_args.args[0] == "task-1"
    assert job_repo.update_job.await_args.kwargs["status"] is DocumentStatus.CANCELLED
    assert job_repo.update_job.await_args.kwargs["completed_at"] is not None


@pytest.mark.asyncio
async def test_cancel_task_leaves_the_durable_job_alone_when_already_terminal() -> None:
    """A cancel that loses the race must not rewrite a COMPLETED job as CANCELLED.

    ``get_object_ref`` still answers for a finished task (the ref is only dropped
    when the entry is evicted), so a late ``DELETE /task/{id}`` reaches this path
    for work that already succeeded. The durable row is the operator-visible
    record of the outcome (#660), so it must reflect what actually happened.
    """
    tsm = _task_state_manager()
    tsm.set_cancelled_if_active = _remote_mock(False)  # worker got there first
    job_repo = _job_repo()
    dispatcher = _dispatcher_with_job_repo(job_repo, tsm=tsm)

    with patch("ray.cancel") as cancel:
        assert await dispatcher.cancel_task("task-1") is False

    job_repo.update_job.assert_not_awaited()
    cancel.assert_not_called()


@pytest.mark.asyncio
async def test_get_task_state_falls_back_to_postgres_after_a_restart() -> None:
    from core.models.catalog import DocumentStatus, IndexationJob

    tsm = _task_state_manager()
    tsm.get_state.remote = AsyncMock(return_value=None)  # cache lost the entry
    job_repo = _job_repo()
    job_repo.get_job = AsyncMock(return_value=IndexationJob(id="task-1", status=DocumentStatus.COMPLETED))
    dispatcher = _dispatcher_with_job_repo(job_repo, tsm=tsm)

    assert await dispatcher.get_task_state("task-1") == "COMPLETED"


@pytest.mark.asyncio
async def test_get_task_error_falls_back_to_postgres_after_a_restart() -> None:
    from core.models.catalog import IndexationJob

    tsm = _task_state_manager()
    tsm.get_error.remote = AsyncMock(return_value=None)
    job_repo = _job_repo()
    job_repo.get_job = AsyncMock(return_value=IndexationJob(id="task-1", error="boom"))
    dispatcher = _dispatcher_with_job_repo(job_repo, tsm=tsm)

    assert await dispatcher.get_task_error("task-1") == "boom"


@pytest.mark.asyncio
async def test_hot_cache_hit_does_not_query_postgres() -> None:
    job_repo = _job_repo()
    dispatcher = _dispatcher_with_job_repo(job_repo)

    assert await dispatcher.get_task_state("task-1") == "SERIALIZING"
    job_repo.get_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_purges_terminal_jobs_at_most_once_per_interval() -> None:
    job_repo = _job_repo()
    dispatcher = _dispatcher_with_job_repo(job_repo)

    for _ in range(3):
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1"},
            partition="tenant-a",
            user=None,
            workspace_ids=None,
            replace=False,
        )

    assert job_repo.purge_terminal_jobs.await_count == 1


@pytest.mark.asyncio
async def test_the_purge_runs_again_once_the_interval_has_passed(monkeypatch) -> None:
    """The throttle must rate-limit the sweep, not disable it after one run.

    Without this, an implementation that purges exactly once per process — and
    so lets the table grow unbounded forever after — passes the at-most-once
    test above.
    """
    from services.workers import dispatcher as dispatcher_module

    clock = {"now": 1_000.0}
    monkeypatch.setattr(dispatcher_module.time, "monotonic", lambda: clock["now"])
    job_repo = _job_repo()
    dispatcher = _dispatcher_with_job_repo(job_repo)

    async def _dispatch():
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1"},
            partition="tenant-a",
            user=None,
            workspace_ids=None,
            replace=False,
        )

    await _dispatch()
    assert job_repo.purge_terminal_jobs.await_count == 1

    clock["now"] += dispatcher_module.JOB_PURGE_INTERVAL_SECONDS - 1
    await _dispatch()
    assert job_repo.purge_terminal_jobs.await_count == 1, "still inside the interval"

    clock["now"] += 2
    await _dispatch()
    assert job_repo.purge_terminal_jobs.await_count == 2, "the interval has elapsed"


@pytest.mark.asyncio
async def test_the_purge_uses_the_documented_retention_bounds() -> None:
    """Pin the shipped retention window and row cap.

    These are the only thing standing between the durable store and the
    unbounded growth #660 exists to fix, and no other test asserts their values.
    """
    job_repo = _job_repo()
    dispatcher = _dispatcher_with_job_repo(job_repo)

    await dispatcher.dispatch_indexing(
        path="/data/report.txt",
        metadata={"file_id": "file-1"},
        partition="tenant-a",
        user=None,
        workspace_ids=None,
        replace=False,
    )

    job_repo.purge_terminal_jobs.assert_awaited_once_with(
        older_than_seconds=7 * 24 * 3600,
        keep_last=10_000,
    )


@pytest.mark.asyncio
async def test_a_failing_purge_is_not_retried_on_every_dispatch() -> None:
    """The throttle timestamp is stamped *before* the sweep, not after it.

    Stamped afterwards, a purge that raises never records an attempt, so every
    subsequent upload pays another failing round-trip to a database that is
    already unhealthy — turning a bounded 5-minute sweep into per-request load
    at exactly the worst moment. ``test_purge_failure_never_fails_a_dispatch``
    covers that the failure is swallowed; this covers that it is not repeated.
    """
    job_repo = _job_repo()
    job_repo.purge_terminal_jobs = AsyncMock(side_effect=RuntimeError("purge blew up"))
    dispatcher = _dispatcher_with_job_repo(job_repo)

    for _ in range(3):
        await dispatcher.dispatch_indexing(
            path="/data/report.txt",
            metadata={"file_id": "file-1"},
            partition="tenant-a",
            user=None,
            workspace_ids=None,
            replace=False,
        )

    assert job_repo.purge_terminal_jobs.await_count == 1, "a failing purge was retried on every dispatch"


@pytest.mark.asyncio
async def test_purge_failure_never_fails_a_dispatch() -> None:
    job_repo = _job_repo()
    job_repo.purge_terminal_jobs = AsyncMock(side_effect=RuntimeError("purge blew up"))
    dispatcher = _dispatcher_with_job_repo(job_repo)

    assert await dispatcher.dispatch_indexing(
        path="/data/report.txt",
        metadata={"file_id": "file-1"},
        partition="tenant-a",
        user=None,
        workspace_ids=None,
        replace=False,
    )


@pytest.mark.asyncio
async def test_cancel_writes_the_durable_row_before_killing_the_worker() -> None:
    """The durable CANCELLED must be written before ``ray.cancel``, not after.

    ``ray.cancel`` kills the only other writer of the row, and the write that
    follows it has no successor that could heal it: ``_record_job`` catches
    ``Exception``, but a client disconnect raises ``asyncio.CancelledError`` —
    a ``BaseException`` — straight through. Writing after the kill therefore
    left the actor CANCELLED and the row stuck on its last active status
    forever: non-terminal, so retention never sweeps it and it is counted
    active for good, while the actor-first and durable-first read paths answer
    differently for the same task id.
    """
    order: list[str] = []
    job_repo = _job_repo()
    job_repo.update_job = AsyncMock(side_effect=lambda *a, **k: order.append("durable"))
    dispatcher = _dispatcher_with_job_repo(job_repo)

    with patch("ray.cancel", side_effect=lambda *a, **k: order.append("ray.cancel")):
        assert await dispatcher.cancel_task("task-1") is True

    assert order == ["durable", "ray.cancel"], order


@pytest.mark.asyncio
async def test_a_cancellation_during_the_durable_write_still_kills_the_worker() -> None:
    """A ``BaseException`` out of the durable write must not skip ``ray.cancel``.

    The user asked for a cancellation and the actor already claimed it; leaving
    the worker running would contradict both records.
    """
    job_repo = _job_repo()
    job_repo.update_job = AsyncMock(side_effect=asyncio.CancelledError())
    dispatcher = _dispatcher_with_job_repo(job_repo)

    with patch("ray.cancel") as cancel:
        with pytest.raises(asyncio.CancelledError):
            await dispatcher.cancel_task("task-1")

    cancel.assert_called_once()
