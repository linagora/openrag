"""Concurrency coverage for atomic catalog deletion accounting."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import pytest
from core.models.catalog import DocumentRecord
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _run_while_user_locked(
    store: PostgresStore,
    user_id: int,
    *operations: Callable[[], Awaitable[Any]],
) -> list[Any]:
    """Release both deletes only after PostgreSQL reports both lock waits."""
    tasks: list[asyncio.Task[Any]] = []
    try:
        async with store.pool.acquire() as blocker:
            async with blocker.transaction():
                await blocker.fetchval("SELECT 1 FROM users WHERE id = $1 FOR UPDATE", user_id)
                tasks = [asyncio.create_task(operation()) for operation in operations]
                try:
                    async with asyncio.timeout(10):
                        while True:
                            # This only yields to the tasks; progress is gated by database locks, not elapsed time.
                            await asyncio.sleep(0)
                            waiters = await blocker.fetchval(
                                """
                                SELECT COUNT(DISTINCT pid)::int
                                FROM pg_locks
                                WHERE granted = false
                                  AND pid <> pg_backend_pid()
                                """
                            )
                            if waiters >= len(operations):
                                break
                except TimeoutError as exc:
                    task_state = [
                        {
                            "done": task.done(),
                            "stack": [f"{frame.f_code.co_name}:{frame.f_lineno}" for frame in task.get_stack()],
                        }
                        for task in tasks
                    ]
                    raise AssertionError({"tasks": task_state}) from exc
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _create_user(store: PostgresStore, name: str) -> int:
    user = await store.user_repo.create_legacy_user(
        display_name=name,
        external_user_id=None,
        email=None,
        is_admin=False,
        file_quota=None,
    )
    return user["id"]


async def _add_file(store: PostgresStore, file_id: str, partition: str, user_id: int) -> None:
    if not await store.partition_repo.partition_exists(partition):
        await store.partition_repo.create_partition(partition)
    assert await store.document_repo.add_file_to_partition(file_id, partition, user_id=user_id) is True


async def _file_count(store: PostgresStore, user_id: int) -> int:
    user = await store.user_repo.get_user_dict_by_id(user_id)
    assert user is not None
    return user["file_count"]


async def test_concurrent_delete_of_the_same_file_decrements_once(postgres_store: PostgresStore):
    user_id = await _create_user(postgres_store, "same-file")
    await _add_file(postgres_store, "target", "race", user_id)
    await _add_file(postgres_store, "control", "control", user_id)

    results = await _run_while_user_locked(
        postgres_store,
        user_id,
        lambda: postgres_store.document_repo.remove_file_from_partition("target", "race"),
        lambda: postgres_store.document_repo.remove_file_from_partition("target", "race"),
    )

    assert sorted(results) == [False, True]
    assert await _file_count(postgres_store, user_id) == 1


async def test_single_file_and_partition_delete_decrement_once(postgres_store: PostgresStore):
    user_id = await _create_user(postgres_store, "single-partition")
    await _add_file(postgres_store, "target", "race", user_id)
    await _add_file(postgres_store, "control", "control", user_id)

    await _run_while_user_locked(
        postgres_store,
        user_id,
        lambda: postgres_store.document_repo.remove_file_from_partition("target", "race"),
        lambda: postgres_store.partition_repo.delete_partition("race"),
    )

    assert await postgres_store.partition_repo.partition_exists("race") is False
    assert await _file_count(postgres_store, user_id) == 1


async def test_single_file_and_bulk_delete_decrement_once(postgres_store: PostgresStore):
    user_id = await _create_user(postgres_store, "single-bulk")
    await _add_file(postgres_store, "target", "race", user_id)
    await _add_file(postgres_store, "control", "control", user_id)

    single_result, bulk_count = await _run_while_user_locked(
        postgres_store,
        user_id,
        lambda: postgres_store.document_repo.remove_file_from_partition("target", "race"),
        lambda: postgres_store.document_repo.delete_documents_by_partition("race"),
    )

    assert int(single_result) + bulk_count == 1
    assert await _file_count(postgres_store, user_id) == 1


async def test_concurrent_partition_deletes_lock_users_in_stable_order(postgres_store: PostgresStore):
    first_user = await _create_user(postgres_store, "first")
    second_user = await _create_user(postgres_store, "second")
    for partition in ("first-partition", "second-partition"):
        await _add_file(postgres_store, f"{partition}-first", partition, first_user)
        await _add_file(postgres_store, f"{partition}-second", partition, second_user)
    await _add_file(postgres_store, "first-control", "control", first_user)
    await _add_file(postgres_store, "second-control", "control", second_user)

    results = await _run_while_user_locked(
        postgres_store,
        first_user,
        lambda: postgres_store.partition_repo.delete_partition("first-partition"),
        lambda: postgres_store.partition_repo.delete_partition("second-partition"),
    )

    assert results == [True, True]
    assert await _file_count(postgres_store, first_user) == 1
    assert await _file_count(postgres_store, second_user) == 1


async def test_concurrent_delete_of_the_same_partition_decrements_once(postgres_store: PostgresStore):
    user_id = await _create_user(postgres_store, "same-partition")
    await _add_file(postgres_store, "target", "race", user_id)
    await _add_file(postgres_store, "control", "control", user_id)

    results = await _run_while_user_locked(
        postgres_store,
        user_id,
        lambda: postgres_store.partition_repo.delete_partition("race"),
        lambda: postgres_store.partition_repo.delete_partition("race"),
    )

    assert sorted(results) == [False, True]
    assert await _file_count(postgres_store, user_id) == 1


async def test_unscoped_delete_removes_one_matching_row_per_call(postgres_store: PostgresStore):
    user_id = await _create_user(postgres_store, "unscoped")
    await _add_file(postgres_store, "shared", "first", user_id)
    await _add_file(postgres_store, "shared", "second", user_id)

    assert await postgres_store.document_repo.delete_document("shared") is True
    assert await _file_count(postgres_store, user_id) == 1
    assert await postgres_store.document_repo.delete_document("shared") is True
    assert await postgres_store.document_repo.delete_document("shared") is False
    assert await _file_count(postgres_store, user_id) == 0


async def test_bulk_delete_ignores_rows_without_an_uploader(postgres_store: PostgresStore):
    user_id = await _create_user(postgres_store, "mixed")
    await _add_file(postgres_store, "owned", "mixed", user_id)
    await postgres_store.document_repo.create_document(DocumentRecord(id="legacy", file_id="legacy", partition="mixed"))

    assert await postgres_store.document_repo.delete_documents_by_partition("mixed") == 2
    assert await _file_count(postgres_store, user_id) == 0


async def test_counter_failure_rolls_back_file_delete(postgres_store: PostgresStore):
    user_id = await _create_user(postgres_store, "rollback")
    await _add_file(postgres_store, "target", "race", user_id)
    await _add_file(postgres_store, "control", "control", user_id)

    async with postgres_store.pool.acquire() as conn:
        await conn.execute(
            """
            CREATE FUNCTION issue724_reject_file_count_update() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'injected file-count failure';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        await conn.execute(
            """
            CREATE TRIGGER issue724_reject_file_count_update
            BEFORE UPDATE OF file_count ON users
            FOR EACH ROW EXECUTE FUNCTION issue724_reject_file_count_update()
            """
        )

    try:
        with pytest.raises(asyncpg.PostgresError, match="injected file-count failure"):
            await postgres_store.document_repo.remove_file_from_partition("target", "race")
    finally:
        async with postgres_store.pool.acquire() as conn:
            await conn.execute("DROP TRIGGER issue724_reject_file_count_update ON users")
            await conn.execute("DROP FUNCTION issue724_reject_file_count_update()")

    assert await postgres_store.document_repo.file_exists_in_partition("target", "race") is True
    assert await _file_count(postgres_store, user_id) == 2
