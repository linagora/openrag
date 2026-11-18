"""File fences (#1430) against a real Postgres: the repository and the catalog commit check."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from core.models.catalog import DocumentStatus, FileFenceKind, IndexationJob
from core.utils.exceptions import FileFenceLostError
from services.persistence.document_repo import _require_file_fence
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

PARTITION = "fenced"
FILE_ID = "doc-1"
HOUR = 3600.0


@pytest_asyncio.fixture(loop_scope="session")
async def store(postgres_store):
    await postgres_store.partition_repo.create_partition(PARTITION)
    return postgres_store


async def _acquire(store, holder_id, *, kind=FileFenceKind.INDEX, ttl=HOUR, file_id=FILE_ID):
    return await store.file_fence_repo.acquire(
        partition=PARTITION, file_id=file_id, kind=kind, holder_id=holder_id, ttl_seconds=ttl
    )


async def _job(store, status: DocumentStatus) -> str:
    job_id = uuid.uuid4().hex
    await store.job_repo.upsert_job(IndexationJob(id=job_id, status=status, partition=PARTITION, file_id=FILE_ID))
    return job_id


async def _file_row(store) -> bool:
    return bool(
        await store.pool.fetchval(
            "SELECT 1 FROM files WHERE partition_name = $1 AND file_id = $2",
            PARTITION,
            FILE_ID,
        )
    )


# ── Acquire ──────────────────────────────────────────────────────────


async def test_a_free_fence_is_taken(store):
    fence = await _acquire(store, "task-a")

    assert fence.holder_id == "task-a"
    assert fence.kind is FileFenceKind.INDEX
    assert await store.file_fence_repo.get(partition=PARTITION, file_id=FILE_ID) == fence


async def test_a_held_fence_reports_its_holder(store):
    await _acquire(store, "task-a")

    fence = await _acquire(store, "task-b", kind=FileFenceKind.METADATA)

    assert fence.holder_id == "task-a"
    assert fence.kind is FileFenceKind.INDEX


async def test_acquiring_again_renews_and_keeps_the_start(store):
    first = await _acquire(store, "task-a", ttl=60)

    again = await _acquire(store, "task-a", ttl=HOUR)

    assert again.holder_id == "task-a"
    assert again.acquired_at == first.acquired_at
    assert again.expires_at > first.expires_at


async def test_an_expired_fence_is_taken(store):
    await _acquire(store, "task-a", ttl=0)

    fence = await _acquire(store, "task-b")

    assert fence.holder_id == "task-b"


@pytest.mark.parametrize("status", [DocumentStatus.COMPLETED, DocumentStatus.FAILED, DocumentStatus.CANCELLED])
async def test_an_index_fence_whose_job_is_over_is_taken(store, status):
    holder = await _job(store, status)
    await _acquire(store, holder)

    fence = await _acquire(store, "task-b")

    assert fence.holder_id == "task-b"


@pytest.mark.parametrize("status", [DocumentStatus.QUEUED, DocumentStatus.SERIALIZING])
async def test_an_index_fence_whose_job_runs_is_kept(store, status):
    holder = await _job(store, status)
    await _acquire(store, holder)

    fence = await _acquire(store, "task-b")

    assert fence.holder_id == holder


async def test_an_index_fence_with_no_job_row_is_kept_until_it_expires(store):
    await _acquire(store, "task-without-job")

    fence = await _acquire(store, "task-b")

    assert fence.holder_id == "task-without-job"


async def test_a_delete_fence_is_not_freed_by_a_job_row(store):
    holder = await _job(store, DocumentStatus.COMPLETED)
    await _acquire(store, holder, kind=FileFenceKind.DELETE)

    fence = await _acquire(store, "task-b")

    assert fence.holder_id == holder


async def test_fences_of_different_files_are_independent(store):
    await _acquire(store, "task-a")

    fence = await _acquire(store, "task-b", file_id="doc-2")

    assert fence.holder_id == "task-b"


async def test_two_processes_racing_for_a_fence_get_one_holder(store, test_rdb_config):
    other = PostgresStore(test_rdb_config, run_migrations=False)
    await other.initialize()
    try:
        holders = [f"task-{i}" for i in range(20)]
        repos = [store.file_fence_repo if i % 2 else other.file_fence_repo for i in range(len(holders))]
        fences = await asyncio.gather(
            *(
                repo.acquire(
                    partition=PARTITION,
                    file_id=FILE_ID,
                    kind=FileFenceKind.INDEX,
                    holder_id=holder,
                    ttl_seconds=HOUR,
                )
                for repo, holder in zip(repos, holders, strict=True)
            )
        )
    finally:
        await other.shutdown()

    winners = {fence.holder_id for fence in fences}
    assert len(winners) == 1
    assert winners <= set(holders)


async def test_a_refusal_names_the_holder_that_won_a_concurrent_take_over(store):
    await _acquire(store, "task-a")
    conn = await store.pool.acquire()
    try:
        transaction = conn.transaction()
        await transaction.start()
        await conn.execute(
            "UPDATE file_fences SET holder_kind = 'delete', holder_id = 'delete-1'"
            " WHERE partition_name = $1 AND file_id = $2",
            PARTITION,
            FILE_ID,
        )
        refused = asyncio.ensure_future(_acquire(store, "task-b"))
        await asyncio.sleep(0.3)
        assert not refused.done()
        await transaction.commit()
    finally:
        await store.pool.release(conn)

    fence = await asyncio.wait_for(refused, timeout=5)

    assert fence.holder_id == "delete-1"
    assert fence.kind is FileFenceKind.DELETE


# ── Take over, renew, release ────────────────────────────────────────


async def test_take_over_replaces_the_expected_holder(store):
    await _acquire(store, "task-a")

    taken = await store.file_fence_repo.take_over(
        partition=PARTITION,
        file_id=FILE_ID,
        kind=FileFenceKind.DELETE,
        holder_id="delete-1",
        expected_holder_id="task-a",
        ttl_seconds=120,
    )

    fence = await store.file_fence_repo.get(partition=PARTITION, file_id=FILE_ID)
    assert taken is True
    assert (fence.holder_id, fence.kind) == ("delete-1", FileFenceKind.DELETE)


async def test_take_over_of_another_holder_changes_nothing(store):
    await _acquire(store, "task-a")

    taken = await store.file_fence_repo.take_over(
        partition=PARTITION,
        file_id=FILE_ID,
        kind=FileFenceKind.DELETE,
        holder_id="delete-1",
        expected_holder_id="task-stale",
        ttl_seconds=120,
    )

    assert taken is False
    assert (await store.file_fence_repo.get(partition=PARTITION, file_id=FILE_ID)).holder_id == "task-a"


async def test_only_the_holder_renews(store):
    first = await _acquire(store, "task-a", ttl=60)
    repo = store.file_fence_repo

    assert await repo.renew(partition=PARTITION, file_id=FILE_ID, holder_id="task-b", ttl_seconds=HOUR) is False
    assert await repo.renew(partition=PARTITION, file_id=FILE_ID, holder_id="task-a", ttl_seconds=HOUR) is True
    assert (await repo.get(partition=PARTITION, file_id=FILE_ID)).expires_at > first.expires_at


async def test_only_the_holder_releases_and_only_once(store):
    await _acquire(store, "task-a")
    repo = store.file_fence_repo

    assert await repo.release(partition=PARTITION, file_id=FILE_ID, holder_id="task-b") is False
    assert await repo.release(partition=PARTITION, file_id=FILE_ID, holder_id="task-a") is True
    assert await repo.release(partition=PARTITION, file_id=FILE_ID, holder_id="task-a") is False
    assert await repo.get(partition=PARTITION, file_id=FILE_ID) is None


async def test_a_released_fence_can_be_taken_again(store):
    await _acquire(store, "task-a")
    await store.file_fence_repo.release(partition=PARTITION, file_id=FILE_ID, holder_id="task-a")

    assert (await _acquire(store, "task-b")).holder_id == "task-b"


async def test_deleting_the_partition_drops_its_fences(store):
    await _acquire(store, "task-a")

    await store.pool.execute("DELETE FROM partitions WHERE partition = $1", PARTITION)

    assert await store.file_fence_repo.get(partition=PARTITION, file_id=FILE_ID) is None


# ── The catalog commit check ─────────────────────────────────────────


async def test_an_insert_by_the_holder_is_written(store):
    await _acquire(store, "task-a")

    written = await store.document_repo.add_file_to_partition(
        file_id=FILE_ID, partition=PARTITION, fence_holder_id="task-a"
    )

    assert written is True
    assert await _file_row(store)


async def test_an_insert_whose_fence_was_taken_over_writes_nothing(store):
    await _acquire(store, "task-a")
    await store.file_fence_repo.take_over(
        partition=PARTITION,
        file_id=FILE_ID,
        kind=FileFenceKind.DELETE,
        holder_id="delete-1",
        expected_holder_id="task-a",
        ttl_seconds=120,
    )

    with pytest.raises(FileFenceLostError):
        await store.document_repo.add_file_to_partition(file_id=FILE_ID, partition=PARTITION, fence_holder_id="task-a")

    assert not await _file_row(store)


async def test_an_insert_with_no_fence_at_all_writes_nothing(store):
    with pytest.raises(FileFenceLostError):
        await store.document_repo.add_file_to_partition(file_id=FILE_ID, partition=PARTITION, fence_holder_id="task-a")

    assert not await _file_row(store)


async def test_an_insert_without_a_holder_skips_the_check(store):
    written = await store.document_repo.add_file_to_partition(file_id=FILE_ID, partition=PARTITION)

    assert written is True


async def test_an_update_by_the_holder_is_written(store):
    await store.document_repo.add_file_to_partition(file_id=FILE_ID, partition=PARTITION)
    await _acquire(store, "task-a")

    updated = await store.document_repo.update_file_in_partition(
        file_id=FILE_ID, partition=PARTITION, chunk_count=7, fence_holder_id="task-a"
    )

    assert updated is True
    assert await store.pool.fetchval("SELECT chunk_count FROM files WHERE file_id = $1", FILE_ID) == 7


async def test_an_update_whose_fence_was_lost_changes_nothing(store):
    await store.document_repo.add_file_to_partition(file_id=FILE_ID, partition=PARTITION, chunk_count=3)
    await _acquire(store, "task-b")

    with pytest.raises(FileFenceLostError):
        await store.document_repo.update_file_in_partition(
            file_id=FILE_ID, partition=PARTITION, chunk_count=7, fence_holder_id="task-a"
        )

    assert await store.pool.fetchval("SELECT chunk_count FROM files WHERE file_id = $1", FILE_ID) == 3


async def test_an_update_with_nothing_to_change_still_checks_the_fence(store):
    await store.document_repo.add_file_to_partition(file_id=FILE_ID, partition=PARTITION)
    await _acquire(store, "task-b")

    with pytest.raises(FileFenceLostError):
        await store.document_repo.update_file_in_partition(
            file_id=FILE_ID, partition=PARTITION, fence_holder_id="task-a"
        )


async def test_a_fenced_commit_during_a_partition_delete_waits_instead_of_deadlocking(store):
    await _acquire(store, "task-a")
    conn = await store.pool.acquire()
    try:
        transaction = conn.transaction()
        await transaction.start()
        # The order of a partition delete: its row first, then the cascade.
        await conn.execute("SELECT partition FROM partitions WHERE partition = $1 FOR UPDATE", PARTITION)
        commit = asyncio.ensure_future(
            store.document_repo.add_file_to_partition(file_id=FILE_ID, partition=PARTITION, fence_holder_id="task-a")
        )
        await asyncio.sleep(0.3)
        await conn.execute("DELETE FROM partitions WHERE partition = $1", PARTITION)
        await transaction.commit()
    finally:
        await store.pool.release(conn)

    with pytest.raises(FileFenceLostError):
        await asyncio.wait_for(commit, timeout=5)
    assert not await _file_row(store)


async def test_a_take_over_waits_for_a_commit_in_flight(store):
    await _acquire(store, "task-a")
    conn = await store.pool.acquire()
    try:
        transaction = conn.transaction()
        await transaction.start()
        await _require_file_fence(conn, PARTITION, FILE_ID, "task-a")
        take_over = asyncio.ensure_future(
            store.file_fence_repo.take_over(
                partition=PARTITION,
                file_id=FILE_ID,
                kind=FileFenceKind.DELETE,
                holder_id="delete-1",
                expected_holder_id="task-a",
                ttl_seconds=120,
            )
        )
        await asyncio.sleep(0.3)
        assert not take_over.done()
        await transaction.commit()
    finally:
        await store.pool.release(conn)

    assert await asyncio.wait_for(take_over, timeout=5) is True


async def test_renew_all_extends_only_the_fences_still_held(store):
    repo = store.file_fence_repo
    first = await _acquire(store, "task-a", ttl=60)
    await _acquire(store, "task-b", ttl=60, file_id="doc-2")
    await repo.take_over(
        partition=PARTITION,
        file_id="doc-2",
        kind=FileFenceKind.DELETE,
        holder_id="delete-1",
        expected_holder_id="task-b",
        ttl_seconds=60,
    )

    renewed = await repo.renew_all(
        [(PARTITION, FILE_ID, "task-a"), (PARTITION, "doc-2", "task-b")],
        ttl_seconds=HOUR,
    )

    assert renewed == 1
    assert (await repo.get(partition=PARTITION, file_id=FILE_ID)).expires_at > first.expires_at
    assert (await repo.get(partition=PARTITION, file_id="doc-2")).holder_id == "delete-1"


async def test_renew_all_skips_a_fence_a_commit_is_holding(store):
    await _acquire(store, "task-a")
    await _acquire(store, "task-b", file_id="doc-2")
    conn = await store.pool.acquire()
    try:
        transaction = conn.transaction()
        await transaction.start()
        await _require_file_fence(conn, PARTITION, FILE_ID, "task-a")

        renewed = await asyncio.wait_for(
            store.file_fence_repo.renew_all(
                [(PARTITION, FILE_ID, "task-a"), (PARTITION, "doc-2", "task-b")], ttl_seconds=HOUR
            ),
            timeout=5,
        )

        await transaction.commit()
    finally:
        await store.pool.release(conn)

    assert renewed == 1


async def test_renew_all_of_nothing_renews_nothing(store):
    assert await store.file_fence_repo.renew_all([], ttl_seconds=HOUR) == 0


async def test_an_expired_fence_still_held_lets_its_holder_commit(store):
    """A lapsed lease only lets someone else in; it never fails its holder by itself."""
    await _acquire(store, "task-a", ttl=0)

    written = await store.document_repo.add_file_to_partition(
        file_id=FILE_ID, partition=PARTITION, fence_holder_id="task-a"
    )

    assert written is True
