"""Phase 7F — PgWorkspaceRepository against a real Postgres."""

from __future__ import annotations

import asyncpg
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
        fetched = await postgres_store.workspace_repo.get_workspace("ws-p", "ws1")
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
        missing = await repo.add_files_to_workspace("ws-p", "ws1", ["f1", "f2"])
        assert missing == []
        files = await repo.list_workspace_files("ws-p", "ws1")
        assert set(files) == {"f1", "f2"}

    async def test_add_reports_unknown_file_ids(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        missing = await repo.add_files_to_workspace("ws-p", "ws1", ["f1", "ghost", "f2"])
        assert missing == ["ghost"]
        assert set(await repo.list_workspace_files("ws-p", "ws1")) == {"f1", "f2"}

    async def test_add_is_idempotent(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.add_files_to_workspace("ws-p", "ws1", ["f1"])
        await repo.add_files_to_workspace("ws-p", "ws1", ["f1"])
        assert await repo.list_workspace_files("ws-p", "ws1") == ["f1"]

    async def test_remove_file_from_workspace(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.add_files_to_workspace("ws-p", "ws1", ["f1", "f2"])
        assert await repo.remove_file_from_workspace("ws-p", "ws1", "f1") is True
        assert await repo.list_workspace_files("ws-p", "ws1") == ["f2"]

    async def test_get_file_workspaces_is_partition_scoped(
        self,
        postgres_store: PostgresStore,
    ):
        await _seed_partition_and_files(postgres_store, partition="a")
        await _seed_partition_and_files(postgres_store, partition="b", file_ids=("f1",))
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws-a", partition="a"))
        await repo.create_workspace(_workspace("ws-b", partition="b"))
        await repo.add_files_to_workspace("a", "ws-a", ["f1"])
        await repo.add_files_to_workspace("b", "ws-b", ["f1"])
        # ``f1`` exists in both partitions as distinct ``files`` rows;
        # the lookup must only return the workspace in partition "a".
        in_a = await repo.get_file_workspaces("f1", "a")
        assert in_a == ["ws-a"]


class TestExistingFileIds:
    """The two catalog-existence lookups behind attachment scoping.

    Exercised against real SQL rather than a fake, so the ``ANY($1::text[])``
    predicate and the presence/absence of the partition clause are actually
    executed — the scoping assertions below both fail if either clause is
    changed.
    """

    async def test_partition_scoped_ignores_other_partitions(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store, partition="a", file_ids=("f1", "f2"))
        await _seed_partition_and_files(postgres_store, partition="b", file_ids=("f3",))
        found = await postgres_store.workspace_repo.get_existing_file_ids("a", ["f1", "f3", "ghost"])
        # f3 exists, but in partition "b" — a partition-scoped lookup must not see it.
        assert found == {"f1"}

    async def test_any_partition_spans_partitions(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store, partition="a", file_ids=("f1",))
        await _seed_partition_and_files(postgres_store, partition="b", file_ids=("f3",))
        found = await postgres_store.workspace_repo.get_existing_file_ids_any_partition(
            ["f1", "f3", "ghost"],
        )
        assert found == {"f1", "f3"}

    async def test_any_partition_reports_a_shared_file_id_once(self, postgres_store: PostgresStore):
        # The same file_id in two partitions is two ``files`` rows. Callers treat
        # the result as a membership set, so it must come back as a single entry.
        # (Both the query's DISTINCT and the set-building enforce this; the test
        # pins the contract, not either mechanism.)
        await _seed_partition_and_files(postgres_store, partition="a", file_ids=("shared",))
        await _seed_partition_and_files(postgres_store, partition="b", file_ids=("shared",))
        found = await postgres_store.workspace_repo.get_existing_file_ids_any_partition(["shared"])
        assert found == {"shared"}

    async def test_empty_input_short_circuits(self, postgres_store: PostgresStore):
        repo = postgres_store.workspace_repo
        assert await repo.get_existing_file_ids("a", []) == set()
        assert await repo.get_existing_file_ids_any_partition([]) == set()


class TestDeleteWorkspace:
    async def test_independently_indexed_files_are_preserved(self, postgres_store: PostgresStore):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.add_files_to_workspace("ws-p", "ws1", ["f1", "f2"])
        await postgres_store.pool.execute("UPDATE files SET independently_indexed = FALSE WHERE file_id = 'f2'")
        orphans = await repo.delete_workspace("ws-p", "ws1")
        assert orphans == ["f2"]
        assert (
            await postgres_store.pool.fetchval(
                "SELECT independently_indexed FROM files WHERE file_id = 'f1'",
            )
            is True
        )
        assert await repo.get_workspace("ws-p", "ws1") is None

    async def test_files_shared_with_other_workspaces_are_not_orphaned(
        self,
        postgres_store: PostgresStore,
    ):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.create_workspace(_workspace("ws2"))
        await postgres_store.pool.execute("UPDATE files SET independently_indexed = FALSE WHERE file_id = 'f1'")
        await repo.add_files_to_workspace("ws-p", "ws1", ["f1"])
        await repo.add_files_to_workspace("ws-p", "ws2", ["f1"])  # f1 shared
        orphans = await repo.delete_workspace("ws-p", "ws1")
        assert orphans == []
        assert await repo.delete_workspace("ws-p", "ws2") == ["f1"]

    async def test_remove_file_from_all_workspaces(
        self,
        postgres_store: PostgresStore,
    ):
        await _seed_partition_and_files(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.create_workspace(_workspace("ws1"))
        await repo.create_workspace(_workspace("ws2"))
        await repo.add_files_to_workspace("ws-p", "ws1", ["f1"])
        await repo.add_files_to_workspace("ws-p", "ws2", ["f1"])
        await repo.remove_file_from_all_workspaces("f1", "ws-p")
        assert await repo.list_workspace_files("ws-p", "ws1") == []
        assert await repo.list_workspace_files("ws-p", "ws2") == []


class TestWorkspaceIdIsScopedToPartition:
    """The same ``workspace_id`` in two partitions is two unrelated workspaces."""

    async def _two_partitions_same_id(self, store: PostgresStore) -> None:
        await _seed_partition_and_files(store, partition="a", file_ids=("f1", "f2"))
        await _seed_partition_and_files(store, partition="b", file_ids=("f1",))
        repo = store.workspace_repo
        await repo.create_workspace(_workspace("shared", partition="a", display_name="in a"))
        await repo.create_workspace(_workspace("shared", partition="b", display_name="in b"))

    async def test_same_id_allowed_across_partitions_not_within_one(self, postgres_store: PostgresStore):
        await self._two_partitions_same_id(postgres_store)
        repo = postgres_store.workspace_repo
        assert (await repo.get_workspace("a", "shared")).display_name == "in a"
        assert (await repo.get_workspace("b", "shared")).display_name == "in b"
        assert await repo.get_workspace("c", "shared") is None
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.create_workspace(_workspace("shared", partition="a"))

    async def test_find_workspaces_filters_by_partition(self, postgres_store: PostgresStore):
        await self._two_partitions_same_id(postgres_store)
        repo = postgres_store.workspace_repo
        assert [w.partition for w in await repo.find_workspaces("shared", None)] == ["a", "b"]
        assert [w.partition for w in await repo.find_workspaces("shared", ["b", "zzz"])] == ["b"]
        assert await repo.find_workspaces("shared", []) == []
        assert await repo.find_workspaces("ghost", None) == []

    async def test_memberships_are_isolated(self, postgres_store: PostgresStore):
        await self._two_partitions_same_id(postgres_store)
        repo = postgres_store.workspace_repo
        assert await repo.add_files_to_workspace("a", "shared", ["f1", "f2"]) == []
        assert await repo.add_files_to_workspace("b", "shared", ["f1"]) == []
        # f2 only exists in partition a: attaching it in b is "not found".
        assert await repo.add_files_to_workspace("b", "shared", ["f2"]) == ["f2"]
        assert set(await repo.list_workspace_files("a", "shared")) == {"f1", "f2"}
        assert await repo.list_workspace_files("b", "shared") == ["f1"]
        assert await repo.get_file_workspaces("f1", "a") == ["shared"]
        assert await repo.get_file_workspaces("f1", "b") == ["shared"]

        assert await repo.remove_file_from_workspace("b", "shared", "f1") is True
        assert await repo.list_workspace_files("b", "shared") == []
        assert set(await repo.list_workspace_files("a", "shared")) == {"f1", "f2"}

    async def test_remove_file_from_all_workspaces_stays_in_its_partition(self, postgres_store: PostgresStore):
        await self._two_partitions_same_id(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.add_files_to_workspace("a", "shared", ["f1"])
        await repo.add_files_to_workspace("b", "shared", ["f1"])
        await repo.remove_file_from_all_workspaces("f1", "a")
        assert await repo.list_workspace_files("a", "shared") == []
        assert await repo.list_workspace_files("b", "shared") == ["f1"]

    async def test_delete_only_touches_the_addressed_partition(self, postgres_store: PostgresStore):
        await self._two_partitions_same_id(postgres_store)
        repo = postgres_store.workspace_repo
        await repo.add_files_to_workspace("a", "shared", ["f1"])
        await repo.add_files_to_workspace("b", "shared", ["f1"])
        await postgres_store.pool.execute("UPDATE files SET independently_indexed = FALSE")

        assert await repo.delete_workspace("a", "shared") == ["f1"]

        assert await repo.get_workspace("a", "shared") is None
        assert (await repo.get_workspace("b", "shared")).display_name == "in b"
        assert await repo.list_workspace_files("b", "shared") == ["f1"]
        assert await repo.delete_workspace("a", "shared") == []  # already gone: no-op
