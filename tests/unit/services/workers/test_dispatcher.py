from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _remote_mock(return_value: Any = None) -> MagicMock:
    method = MagicMock()
    method.remote = AsyncMock(return_value=return_value)
    return method


def _pool_with_ref(ref: object) -> MagicMock:
    pool = MagicMock()
    pool.process_file = MagicMock()
    pool.process_file.remote = MagicMock(return_value=ref)
    return pool


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
    tsm.set_details = _remote_mock()
    tsm.set_object_ref = _remote_mock()
    tsm.get_state = _remote_mock("SERIALIZING")
    tsm.get_error = _remote_mock("traceback")
    tsm.get_object_ref = _remote_mock({"ref": object()})
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
    pool.process_file.remote.assert_called_once_with(
        task_id="task-1",
        path="/data/report.txt",
        metadata={"file_id": "file-1", "source": "/data/report.txt", "filename": "report.txt"},
        partition="tenant-a",
        user={"id": 42},
        workspace_ids=["ws-1"],
        replace=True,
    )
    tsm.set_object_ref.remote.assert_called_once_with("task-1", {"ref": ref})


@pytest.mark.asyncio
async def test_worker_dispatcher_mutates_files_without_legacy_indexer() -> None:
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

    await dispatcher.delete_file("file-1", "tenant-a")
    await dispatcher.update_file_metadata("file-1", {"title": "new"}, "tenant-a", user={"id": 7})
    await dispatcher.copy_file("file-1", {"file_id": "copy-1", "partition": "tenant-b"}, "tenant-b", user=None)

    vector_store.query_ids_by_filter.assert_called_once_with("default", {"partition": "tenant-a", "file_id": "file-1"})
    vector_store.delete.assert_called_once_with(["1", "2"], "default")
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


@pytest.mark.asyncio
async def test_cancel_task_uses_stored_pool_object_ref() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    tsm = _task_state_manager()
    tsm.get_object_ref.remote = AsyncMock(return_value={"ref": ref})
    tsm.get_state.remote = AsyncMock(return_value="SERIALIZING")
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
    cancel.assert_called_once_with(ref, recursive=True)
    tsm.set_state.remote.assert_called_once_with("task-1", "CANCELLED")


@pytest.mark.asyncio
async def test_cancel_task_marks_cancelled_even_if_worker_finished_first() -> None:
    from services.workers.dispatcher import WorkerDispatcher

    ref = object()
    tsm = _task_state_manager()
    tsm.get_object_ref.remote = AsyncMock(return_value={"ref": ref})
    tsm.get_state.remote = AsyncMock(return_value="COMPLETED")
    dispatcher = WorkerDispatcher(
        pool=_pool_with_ref(object()),
        task_state_manager=tsm,
        vector_store=_vector_store(),
        document_repo=_document_repo(),
        workspace_repo=_workspace_repo(),
        collection="default",
    )

    with patch("ray.cancel"):
        result = await dispatcher.cancel_task("task-1")

    assert result is True
    tsm.set_state.remote.assert_called_once_with("task-1", "CANCELLED")
