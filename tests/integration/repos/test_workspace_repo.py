"""Phase 7F — PgWorkspaceRepository against a real Postgres."""

from __future__ import annotations

import pytest
from core.models.catalog import DocumentRecord
from core.models.workspace import Workspace
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _seed_partition_and_files(
    store: PostgresStore,
    partition: str = "ws-p",
    file_ids: tuple[str, ...] = ("f1", "f2", "f3"),
) -> str:
    await store.partition_repo.create_partition(partition)
    for fid in file_ids:
        await store.document_repo.create_document(
            DocumentRecord(id=fid, file_id=fid, partition=partition, filename=f"{fid}.pdf"),
        )
    return partition


def _workspace(workspace_id: str = "ws1", partition: str = "ws-p", **extra) -> Workspace:
    return Workspace(workspace_id=workspace_id, partition=partition, **extra)


class TestCreateGetList:
    async def test_create_then_get(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        await postgres_store.workspace_repo.create_workspace(
            _workspace("ws1", display_name="My workspace"),
        )
        fetched = await postgres_store.workspace_repo.get_workspace("ws1")
        assert fetched is not None
        assert fetched.workspace_id == "ws1"
        assert fetched.display_name == "My workspace"

    async def test_list_filters_by_partition(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store, partition="a")
        await _seed_partition_and_files(postgres_store, partition="b", file_ids=("b1",))
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws-a", partition="a"))
        await repo.create_workspace(_workspace("ws-b", partition="b"))
        only_a = await repo.list_workspaces("a")
        assert {w.workspace_id for w in only_a} == {"ws-a"}


class TestFileMembership:
    async def test_add_then_list_workspace_files(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        missing = await repo.add_files_to_workspace("ws1", ["f1", "f2"])
        assert missing == []
        files = await repo.list_workspace_files("ws1")
        assert set(files) == {"f1", "f2"}

    async def test_add_reports_unknown_file_ids(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        missing = await repo.add_files_to_workspace("ws1", ["f1", "ghost", "f2"])
        assert missing == ["ghost"]
        assert set(await repo.list_workspace_files("ws1")) == {"f1", "f2"}

    async def test_add_is_idempotent(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.add_files_to_workspace("ws1", ["f1"])
        await repo.add_files_to_workspace("ws1", ["f1"])
        assert await repo.list_workspace_files("ws1") == ["f1"]

    async def test_remove_file_from_workspace(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.add_files_to_workspace("ws1", ["f1", "f2"])
        assert await repo.remove_file_from_workspace("ws1", "f1") is True
        assert await repo.list_workspace_files("ws1") == ["f2"]

    async def test_get_file_workspaces_is_partition_scoped(
        self,
        postgres_store: PostgresStore,
    ):
        await _seed_partition_and_files(postgres_store, partition="a")
        await _seed_partition_and_files(postgres_store, partition="b", file_ids=("f1",))
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws-a", partition="a"))
        await repo.create_workspace(_workspace("ws-b", partition="b"))
        await repo.add_files_to_workspace("ws-a", ["f1"])
        await repo.add_files_to_workspace("ws-b", ["f1"])
        # ``f1`` exists in both partitions as distinct ``files`` rows;
        # the lookup must only return the workspace in partition "a".
        in_a = await repo.get_file_workspaces("f1", "a")
        assert in_a == ["ws-a"]


class TestDeleteWorkspace:
    async def test_returns_orphan_file_ids(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.add_files_to_workspace("ws1", ["f1", "f2"])
        orphans = await repo.delete_workspace("ws1")
        # Both files were only in ws1 — both come back as orphans.
        assert set(orphans) == {"f1", "f2"}
        assert await repo.get_workspace("ws1") is None

    async def test_files_shared_with_other_workspaces_are_not_orphaned(
        self,
        postgres_store: PostgresStore,
    ):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.create_workspace(_workspace("ws2"))
        await repo.add_files_to_workspace("ws1", ["f1", "f2"])
        await repo.add_files_to_workspace("ws2", ["f1"])  # f1 shared
        orphans = await repo.delete_workspace("ws1")
        # f1 is still in ws2 so it's not orphaned. f2 only lived in ws1.
        assert set(orphans) == {"f2"}

    async def test_remove_file_from_all_workspaces(
        self,
        postgres_store: PostgresStore,
    ):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.create_workspace(_workspace("ws2"))
        await repo.add_files_to_workspace("ws1", ["f1"])
        await repo.add_files_to_workspace("ws2", ["f1"])
        await repo.remove_file_from_all_workspaces("f1", "ws-p")
        assert await repo.list_workspace_files("ws1") == []
        assert await repo.list_workspace_files("ws2") == []
