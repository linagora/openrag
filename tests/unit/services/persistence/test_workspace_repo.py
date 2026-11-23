"""Unit tests for workspace repository concurrency guards."""

from datetime import UTC, datetime

import pytest
from services.persistence.workspace_repo import PgWorkspaceRepository


@pytest.mark.asyncio
async def test_delete_lookup_matches_the_confirmed_workspace_instance():
    expected_created_at = datetime(2025, 1, 10, 9, 0, tzinfo=UTC)

    class Connection:
        query = ""
        args: tuple[object, ...] = ()

        async def fetchval(self, query: str, *args: object) -> int:
            self.query = query
            self.args = args
            return 17

    connection = Connection()

    workspace_pk = await PgWorkspaceRepository._workspace_pk(
        connection,
        "legal",
        "contracts",
        for_update=True,
        expected_created_at=expected_created_at,
    )

    assert workspace_pk == 17
    assert connection.query == (
        "SELECT id FROM workspaces WHERE partition_name = $1 AND workspace_id = $2 AND created_at = $3 FOR UPDATE"
    )
    assert connection.args == ("legal", "contracts", expected_created_at)


@pytest.mark.asyncio
async def test_lookup_matches_a_legacy_workspace_with_null_created_at():
    class Connection:
        query = ""
        args: tuple[object, ...] = ()

        async def fetchval(self, query: str, *args: object) -> int:
            self.query = query
            self.args = args
            return 17

    connection = Connection()
    workspace_pk = await PgWorkspaceRepository._workspace_pk(
        connection,
        "legal",
        "contracts",
        for_key_share=True,
        expected_created_at_is_null=True,
    )

    assert workspace_pk == 17
    assert connection.query == (
        "SELECT id FROM workspaces WHERE partition_name = $1 AND workspace_id = $2 AND created_at IS NULL FOR KEY SHARE"
    )
    assert connection.args == ("legal", "contracts")


def test_row_to_workspace_accepts_a_legacy_null_created_at():
    workspace = PgWorkspaceRepository._row_to_workspace(
        {
            "workspace_id": "contracts",
            "partition_name": "legal",
            "display_name": None,
            "created_by": 1,
            "created_at": None,
        }
    )

    assert workspace.created_at is None


@pytest.mark.asyncio
async def test_list_workspace_files_reads_generation_and_membership_together():
    expected_created_at = datetime(2025, 1, 10, 9, 0)

    class Pool:
        query = ""
        args: tuple[object, ...] = ()

        async def fetch(self, query: str, *args: object) -> list[dict]:
            self.query = query
            self.args = args
            return [
                {"created_at": expected_created_at, "file_id": "f1"},
                {"created_at": expected_created_at, "file_id": "f2"},
            ]

    pool = Pool()
    repository = PgWorkspaceRepository(lambda: pool)

    file_ids = await repository.list_workspace_files(
        "legal",
        "contracts",
        expected_created_at=expected_created_at,
    )

    assert file_ids == ["f1", "f2"]
    assert "LEFT JOIN workspace_files" in pool.query
    assert "LEFT JOIN files" in pool.query
    assert pool.args == ("legal", "contracts")


@pytest.mark.asyncio
async def test_list_workspace_files_returns_conflict_for_replacement_generation():
    class Pool:
        async def fetch(self, _query: str, *_args: object) -> list[dict]:
            return [{"created_at": datetime(2025, 2, 10, 9, 0), "file_id": "replacement-file"}]

    repository = PgWorkspaceRepository(lambda: Pool())

    file_ids = await repository.list_workspace_files(
        "legal",
        "contracts",
        expected_created_at=datetime(2025, 1, 10, 9, 0),
    )

    assert file_ids is None


@pytest.mark.asyncio
async def test_list_workspace_files_matches_legacy_null_generation_and_empty_membership():
    class Pool:
        async def fetch(self, _query: str, *_args: object) -> list[dict]:
            return [{"created_at": None, "file_id": None}]

    repository = PgWorkspaceRepository(lambda: Pool())

    file_ids = await repository.list_workspace_files(
        "legal",
        "contracts",
        expected_created_at_is_null=True,
    )

    assert file_ids == []


@pytest.mark.asyncio
async def test_list_workspace_files_reports_a_workspace_missing_during_the_read():
    class Pool:
        async def fetch(self, _query: str, *_args: object) -> list[dict]:
            return []

    repository = PgWorkspaceRepository(lambda: Pool())

    file_ids = await repository.list_workspace_files("legal", "contracts")

    assert file_ids is None
