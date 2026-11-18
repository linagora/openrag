"""Races between file operations, through the dispatcher and a real Postgres (#1430).

The worker is a stub: the tests play its catalog write by hand, with the
holder the dispatcher handed it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from core.utils.exceptions import ConflictError, FileFenceLostError
from services.workers.dispatcher import WorkerDispatcher

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

PARTITION = "fenced-dispatch"
FILE_ID = "doc-1"


@pytest_asyncio.fixture(loop_scope="session")
async def store(postgres_store):
    await postgres_store.partition_repo.create_partition(PARTITION)
    return postgres_store


def _remote_mock(return_value: Any = None) -> MagicMock:
    method = MagicMock()
    method.remote = AsyncMock(return_value=return_value)
    return method


def _dispatcher(store, *, tsm: MagicMock | None = None) -> tuple[WorkerDispatcher, MagicMock]:
    if tsm is None:
        tsm = MagicMock()
        tsm.set_queued_details_v2 = _remote_mock({"accepted": True, "reason": None, "existing_task_id": None})
        tsm.begin_worker_submission = _remote_mock(True)
        tsm.get_matching_active_task_refs_v2 = _remote_mock({})
        tsm.begin_file_delete = _remote_mock()
        tsm.renew_file_delete = _remote_mock(True)
        tsm.end_file_delete = _remote_mock()
    pool = MagicMock()
    pool.submit = _remote_mock([object()])
    tracker = MagicMock()
    tracker.track.remote.return_value = object()
    vector_store = MagicMock()
    vector_store.collection_exists = AsyncMock(return_value=True)
    vector_store.delete_by_filter = AsyncMock(return_value=0)
    dispatcher = WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm,
        completion_tracker=tracker,
        vector_store=vector_store,
        document_repo=store.document_repo,
        workspace_repo=store.workspace_repo,
        collection="default",
        file_fence_repo=store.file_fence_repo,
        timeout=5.0,
    )
    return dispatcher, pool


async def _upload(dispatcher: WorkerDispatcher, *, replace: bool) -> str:
    return await dispatcher.dispatch_indexing(
        path="/data/doc.txt",
        metadata={"file_id": FILE_ID, "filename": "doc.txt"},
        partition=PARTITION,
        user=None,
        workspace_ids=None,
        replace=replace,
    )


async def _commit_as_worker(store, holder_id: str) -> bool:
    return await store.document_repo.add_file_to_partition(
        file_id=FILE_ID, partition=PARTITION, fence_holder_id=holder_id
    )


async def _file_row(store) -> bool:
    return bool(
        await store.pool.fetchval(
            "SELECT 1 FROM files WHERE partition_name = $1 AND file_id = $2",
            PARTITION,
            FILE_ID,
        )
    )


async def test_a_second_replace_of_a_file_being_replaced_is_refused(store):
    first, _ = _dispatcher(store)
    second, second_pool = _dispatcher(store)
    task_id = await _upload(first, replace=True)

    with pytest.raises(ConflictError) as caught:
        await _upload(second, replace=True)

    assert caught.value.code == "DOCUMENT_INDEXING_IN_PROGRESS"
    assert caught.value.extra["existing_task_id"] == task_id
    second_pool.submit.remote.assert_not_awaited()


async def test_a_file_deleted_while_it_was_indexing_is_not_written_back(store):
    dispatcher, pool = _dispatcher(store)
    task_id = await _upload(dispatcher, replace=False)
    holder = pool.submit.remote.call_args.kwargs["fence_holder_id"]

    await dispatcher.delete_file(FILE_ID, PARTITION)

    with pytest.raises(FileFenceLostError):
        await _commit_as_worker(store, holder)
    assert holder == task_id
    assert not await _file_row(store)
    assert await store.file_fence_repo.get(partition=PARTITION, file_id=FILE_ID) is None


async def test_an_upload_during_a_delete_is_refused(store):
    delete_started = asyncio.Event()
    finish_delete = asyncio.Event()
    deleting, _ = _dispatcher(store)

    async def slow_delete(**_kwargs: Any) -> dict:
        delete_started.set()
        await finish_delete.wait()
        return {}

    # Parked once it holds the fence, while it cancels the file's tasks.
    deleting._tsm.get_matching_active_task_refs_v2.remote.side_effect = slow_delete
    delete = asyncio.create_task(deleting.delete_file(FILE_ID, PARTITION))
    await asyncio.wait_for(delete_started.wait(), timeout=5)
    uploading, uploading_pool = _dispatcher(store)

    try:
        with pytest.raises(ConflictError) as caught:
            await _upload(uploading, replace=False)
    finally:
        finish_delete.set()
        await delete

    assert caught.value.code == "DOCUMENT_DELETE_IN_PROGRESS"
    uploading_pool.submit.remote.assert_not_awaited()


async def test_a_file_is_free_again_once_the_worker_releases_it(store):
    dispatcher, pool = _dispatcher(store)
    await _upload(dispatcher, replace=False)
    holder = pool.submit.remote.call_args.kwargs["fence_holder_id"]
    assert await _commit_as_worker(store, holder) is True
    await store.file_fence_repo.release(partition=PARTITION, file_id=FILE_ID, holder_id=holder)

    again, _ = _dispatcher(store)
    await _upload(again, replace=True)


# ── PR 3: metadata update, copy, workspace delete ────────────────────


def _with_chunks(dispatcher: WorkerDispatcher, file_id: str) -> MagicMock:
    store = dispatcher._vector_store
    store.query_chunks_by_filter = AsyncMock(return_value=[{"_id": 1, "text": "a", "file_id": file_id}])
    store.upsert_entities = AsyncMock()
    store.insert_entities = AsyncMock()
    return store


async def test_a_metadata_update_during_a_delete_is_refused_and_rewrites_no_chunk(store):
    file_id = "patched-1"
    await store.document_repo.add_file_to_partition(file_id=file_id, partition=PARTITION)
    delete_started = asyncio.Event()
    finish_delete = asyncio.Event()
    deleting, _ = _dispatcher(store)

    async def slow_delete(**_kwargs: Any) -> dict:
        delete_started.set()
        await finish_delete.wait()
        return {}

    deleting._tsm.get_matching_active_task_refs_v2.remote.side_effect = slow_delete
    delete = asyncio.create_task(deleting.delete_file(file_id, PARTITION))
    await asyncio.wait_for(delete_started.wait(), timeout=5)
    patching, _ = _dispatcher(store)
    chunks = _with_chunks(patching, file_id)

    try:
        with pytest.raises(ConflictError) as caught:
            await patching.update_file_metadata(file_id, {"title": "new"}, PARTITION, None)
    finally:
        finish_delete.set()
        await delete

    assert caught.value.code == "DOCUMENT_DELETE_IN_PROGRESS"
    chunks.upsert_entities.assert_not_awaited()


async def test_a_delete_during_a_metadata_update_waits_for_it(store):
    file_id = "patched-2"
    await store.document_repo.add_file_to_partition(file_id=file_id, partition=PARTITION)
    patching, _ = _dispatcher(store)
    chunks = _with_chunks(patching, file_id)
    upsert_started = asyncio.Event()
    finish_upsert = asyncio.Event()
    order: list[str] = []

    async def slow_upsert(*_args: Any, **_kwargs: Any) -> None:
        upsert_started.set()
        await finish_upsert.wait()
        order.append("chunks rewritten")

    chunks.upsert_entities.side_effect = slow_upsert
    patch = asyncio.create_task(patching.update_file_metadata(file_id, {"title": "new"}, PARTITION, None))
    await asyncio.wait_for(upsert_started.wait(), timeout=5)
    deleting, _ = _dispatcher(store)
    deleting._vector_store.delete_by_filter.side_effect = lambda *_a, **_k: order.append("chunks deleted") or 0

    delete = asyncio.create_task(deleting.delete_file(file_id, PARTITION))
    await asyncio.sleep(0.5)
    assert order == []
    finish_upsert.set()
    await asyncio.wait_for(asyncio.gather(patch, delete), timeout=10)

    assert order[0] == "chunks rewritten"
    assert "chunks deleted" in order


async def test_a_copy_of_a_file_being_replaced_is_refused(store):
    await store.document_repo.add_file_to_partition(file_id=FILE_ID, partition=PARTITION)
    replacing, _ = _dispatcher(store)
    await _upload(replacing, replace=True)
    copying, _ = _dispatcher(store)
    chunks = _with_chunks(copying, FILE_ID)

    with pytest.raises(ConflictError) as caught:
        await copying.copy_file(FILE_ID, {"file_id": "copy-1", "partition": PARTITION}, PARTITION, None)

    assert caught.value.code == "DOCUMENT_INDEXING_IN_PROGRESS"
    chunks.insert_entities.assert_not_awaited()
    assert await store.file_fence_repo.get(partition=PARTITION, file_id="copy-1") is None


async def test_a_workspace_delete_during_a_replace_leaves_the_replace_nothing_to_commit(store):
    from services.orchestrators.workspace_service import WorkspaceService

    file_id = "orphaned-1"
    await store.workspace_repo.create_workspace_legacy("ws-1", PARTITION, None)
    await store.document_repo.add_file_to_partition(file_id=file_id, partition=PARTITION, independently_indexed=False)
    await store.workspace_repo.add_files_to_workspace(PARTITION, "ws-1", [file_id])
    replacing, pool = _dispatcher(store)
    await replacing.dispatch_indexing(
        path="/data/doc.txt",
        metadata={"file_id": file_id, "filename": "doc.txt"},
        partition=PARTITION,
        user=None,
        workspace_ids=None,
        replace=True,
    )
    holder = pool.submit.remote.call_args.kwargs["fence_holder_id"]
    vectors = MagicMock()
    vectors.query_ids_by_filter = AsyncMock(return_value=[])
    vectors.delete = AsyncMock(return_value=0)
    workspaces = WorkspaceService(
        workspace_repo=store.workspace_repo,
        document_repo=store.document_repo,
        vector_store=vectors,
        collection="default",
        file_fence_repo=store.file_fence_repo,
    )

    out = await workspaces.delete_workspace(PARTITION, "ws-1")

    assert out["orphaned_files_deleted"] == 1
    with pytest.raises(FileFenceLostError):
        await store.document_repo.update_file_in_partition(
            file_id=file_id, partition=PARTITION, chunk_count=1, fence_holder_id=holder
        )
    assert not await store.pool.fetchval(
        "SELECT 1 FROM files WHERE partition_name = $1 AND file_id = $2", PARTITION, file_id
    )
