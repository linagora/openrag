"""Phase 7F — PgPartitionRepository against a real Postgres."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest
from core.utils.exceptions import ServiceUnavailableError
from services.persistence.partition_repo import _PARTITION_COPY_LOCK_NAMESPACE
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


class TestCreateList:
    async def test_create_then_get(self, postgres_store: PostgresStore):
        repo = postgres_store.partition_repo
        created = await repo.create_partition("alpha")
        assert created["partition"] == "alpha"
        # ``created_at`` comes from the DB default.
        assert created.get("created_at")

    async def test_list_returns_all_known(self, postgres_store: PostgresStore):
        repo = postgres_store.partition_repo
        await repo.create_partition("p1")
        await repo.create_partition("p2")
        names = {row["partition"] for row in await repo.list_partitions()}
        assert {"p1", "p2"} <= names

    async def test_create_is_idempotent_per_name(self, postgres_store: PostgresStore):
        repo = postgres_store.partition_repo
        await repo.create_partition("dup")
        # The legacy method swallows the conflict and returns the existing row
        # rather than raising. Orchestrators rely on this for "ensure exists".
        await repo.create_partition("dup")
        assert await repo.partition_exists("dup") is True
        assert len([r for r in await repo.list_partitions() if r["partition"] == "dup"]) == 1


class TestExistsCounts:
    async def test_partition_exists_returns_false_for_missing(
        self,
        postgres_store: PostgresStore,
    ):
        repo = postgres_store.partition_repo
        assert await repo.partition_exists("never-created") is False

    async def test_total_file_count_starts_at_zero(self, postgres_store: PostgresStore):
        repo = postgres_store.partition_repo
        assert await repo.get_total_file_count() == 0


class TestDelete:
    async def test_delete_removes_the_partition_row(
        self,
        postgres_store: PostgresStore,
    ):
        repo = postgres_store.partition_repo
        await repo.create_partition("doomed")
        assert await repo.partition_exists("doomed") is True
        removed = await repo.delete_partition("doomed")
        assert removed is True
        assert await repo.partition_exists("doomed") is False

    async def test_delete_missing_returns_false(self, postgres_store: PostgresStore):
        repo = postgres_store.partition_repo
        assert await repo.delete_partition("ghost") is False

    async def test_delete_cascades_files_and_decrements_uploader_count(
        self,
        postgres_store: PostgresStore,
    ):
        """Regression: ``files.partition_name`` has no DB-level CASCADE, so the
        repo must delete file rows itself before dropping the partition. Also
        verifies the per-uploader ``file_count`` decrement.
        """
        partition_repo = postgres_store.partition_repo
        document_repo = postgres_store.document_repo
        user_repo = postgres_store.user_repo

        uploader = await user_repo.create_legacy_user(display_name="Uploader")
        uploader_id = uploader["id"]

        await partition_repo.create_partition("cascade-me")
        await document_repo.add_file_to_partition(
            file_id="f1",
            partition="cascade-me",
            user_id=uploader_id,
        )
        await document_repo.add_file_to_partition(
            file_id="f2",
            partition="cascade-me",
            user_id=uploader_id,
        )
        assert await partition_repo.get_partition_file_count("cascade-me") == 2

        assert await partition_repo.delete_partition("cascade-me") is True

        assert await partition_repo.partition_exists("cascade-me") is False
        assert await partition_repo.get_partition_file_count("cascade-me") == 0
        refreshed = await user_repo.get_user_dict_by_id(uploader_id)
        assert refreshed["file_count"] == 0


class TestGenerationPromptNames:
    async def test_round_trip_and_default_empty(self, postgres_store: PostgresStore):
        repo = postgres_store.partition_repo
        await repo.create_partition("genp")
        # Defaults to an empty JSONB map.
        row = await repo.get_partition_row("genp")
        assert row["generation_prompt_names"] == {}
        # Update persists and reads back as a dict (jsonb codec).
        await repo.update_partition("genp", generation_prompt_names={"sys_prompt": "legal"})
        row = await repo.get_partition_row("genp")
        assert row["generation_prompt_names"] == {"sys_prompt": "legal"}


class TestCopyLock:
    async def test_a_copy_in_flight_is_seen_without_waiting(self, postgres_store: PostgresStore):
        repo = postgres_store.partition_repo
        assert await repo.copy_in_progress("p1") is False

        # Concurrent copies into one partition don't wait on each other.
        async with repo.copy_lock("p1"), repo.copy_lock("p1"):
            assert await repo.copy_in_progress("p1") is True
            assert await repo.copy_in_progress("p2") is False

        assert await repo.copy_in_progress("p1") is False

    async def test_copies_leave_the_request_pool_alone(self, test_rdb_config):
        # A copy can run for minutes: a pool connection per copy would let a
        # few large ones starve every request.
        config = test_rdb_config.model_copy(update={"pool_min_size": 1, "pool_max_size": 2})
        store = PostgresStore(config, run_migrations=False)
        await store.initialize()
        try:
            async with asyncio.timeout(10), AsyncExitStack() as copies:
                for _ in range(5):
                    await copies.enter_async_context(store.partition_repo.copy_lock("p1"))
                async with store.pool.acquire() as first, store.pool.acquire() as second:
                    assert [await first.fetchval("SELECT 1"), await second.fetchval("SELECT 1")] == [1, 1]
                assert await store.partition_repo.copy_in_progress("p1") is True
            assert await store.partition_repo.copy_in_progress("p1") is False
        finally:
            await store.shutdown()

    async def test_a_copy_that_loses_its_lock_is_stopped(self, postgres_store: PostgresStore):
        # Its lock went with the session: finishing the copy would let an
        # embedder change slip in meanwhile.
        repo = postgres_store.partition_repo
        locked = asyncio.Event()

        async def copy() -> None:
            async with repo.copy_lock("p1"):
                locked.set()
                await asyncio.Event().wait()

        running = asyncio.create_task(copy())
        await locked.wait()
        pid = await postgres_store.pool.fetchval(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND mode = 'ShareLock' AND classid::bigint = $1",
            _PARTITION_COPY_LOCK_NAMESPACE,
        )
        await postgres_store.pool.execute("SELECT pg_terminate_backend($1)", pid)

        with pytest.raises(ServiceUnavailableError, match="Retry the copy"):
            async with asyncio.timeout(10):
                await running
        assert await repo.copy_in_progress("p1") is False

        # The next copy locks on a new session.
        async with repo.copy_lock("p1"):
            assert await repo.copy_in_progress("p1") is True
        assert await repo.copy_in_progress("p1") is False
