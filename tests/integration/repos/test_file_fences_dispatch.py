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
