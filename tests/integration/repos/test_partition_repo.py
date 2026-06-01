"""Phase 7F — PgPartitionRepository against a real Postgres."""

from __future__ import annotations

import pytest
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
