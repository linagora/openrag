"""Workspace deletion through the production service, catalog writer, and SQL."""

from unittest.mock import AsyncMock

import pytest
from core.models.workspace import Workspace
from services.orchestrators.workspace_service import WorkspaceService
from services.workers.indexer_actor import _write_catalog_record

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def upload(store, file_id, *, partition="p", workspace_ids=None, replace=False):
    assert await _write_catalog_record(
        doc_repo=store.document_repo,
        metadata={"file_id": file_id},
        partition=partition,
        user=None,
        replace=replace,
        indexation_config=None,
        workspace_ids=workspace_ids,
    )
    if not replace:
        for workspace_id in workspace_ids or []:
            assert await store.workspace_repo.add_files_to_workspace(workspace_id, [file_id]) == []


async def setup_workspace(store, workspace_id="ws1", partition="p"):
    await store.partition_repo.create_partition(partition)
    await store.workspace_repo.create_workspace(Workspace(workspace_id=workspace_id, partition=partition))


def service(store):
    vectors = AsyncMock()
    vectors.query_ids_by_filter.side_effect = lambda collection, filters: [filters["file_id"] + "-chunk"]
    return WorkspaceService(
        workspace_repo=store.workspace_repo,
        document_repo=store.document_repo,
        vector_store=vectors,
        collection="test",
    ), vectors


async def test_mixed_workspace_preserves_partition_files_and_shared_uploads(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await store.workspace_repo.create_workspace(Workspace(workspace_id="ws2", partition="p"))
    await upload(store, "independent")
    await store.workspace_repo.add_files_to_workspace("ws1", ["independent"])
    await upload(store, "exclusive", workspace_ids=["ws1"])
    await upload(store, "shared", workspace_ids=["ws1", "ws2"])
    svc, vectors = service(store)

    result = await svc.delete_workspace("p", "ws1")

    assert result == {"orphaned_files_deleted": 1, "orphaned_files_failed": [], "kept_files": 0}
    assert await store.workspace_repo.get_workspace("ws1") is None
    assert await store.workspace_repo.list_workspace_files("ws1") == []
    assert await store.document_repo.file_exists_in_partition("independent", "p")
    assert await store.document_repo.file_exists_in_partition("shared", "p")
    assert not await store.document_repo.file_exists_in_partition("exclusive", "p")
    vectors.delete.assert_awaited_once_with(["exclusive-chunk"], "test")

    result = await svc.delete_workspace("p", "ws2")
    assert result["orphaned_files_deleted"] == 1
    assert not await store.document_repo.file_exists_in_partition("shared", "p")


@pytest.mark.parametrize("shared", [False, True])
async def test_keep_files_preserves_upload_after_later_workspace_deletion(postgres_store, shared):
    store = postgres_store
    await setup_workspace(store)
    await store.workspace_repo.create_workspace(Workspace(workspace_id="ws2", partition="p"))
    await upload(store, "kept", workspace_ids=["ws1", "ws2"] if shared else ["ws1"])
    svc, vectors = service(store)

    result = await svc.delete_workspace("p", "ws1", keep_files=True)
    assert result["kept_files"] == (0 if shared else 1)
    await store.workspace_repo.add_files_to_workspace("ws2", ["kept"])
    await svc.delete_workspace("p", "ws2")

    assert await store.document_repo.file_exists_in_partition("kept", "p")
    vectors.delete.assert_not_awaited()


@pytest.mark.parametrize("workspace_owned", [False, True])
async def test_replacement_preserves_original_ownership(postgres_store, workspace_owned):
    store = postgres_store
    await setup_workspace(store)
    await upload(store, "file", workspace_ids=["ws1"] if workspace_owned else None)
    await store.workspace_repo.add_files_to_workspace("ws1", ["file"])
    await upload(store, "file", replace=True)
    svc, _ = service(store)
    result = await svc.delete_workspace("p", "ws1")
    assert result["orphaned_files_deleted"] == int(workspace_owned)
    assert bool(await store.document_repo.file_exists_in_partition("file", "p")) is not workspace_owned


async def test_same_file_id_in_another_partition_is_untouched(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await setup_workspace(store, "other", "q")
    await upload(store, "file", workspace_ids=["ws1"])
    await upload(store, "file", partition="q")
    svc, vectors = service(store)
    await svc.delete_workspace("p", "ws1")
    assert await store.document_repo.file_exists_in_partition("file", "q")
    assert not await store.document_repo.file_exists_in_partition("file", "p")
    vectors.query_ids_by_filter.assert_awaited_once_with("test", {"partition": "p", "file_id": "file"})


async def test_empty_and_missing_workspaces_return_no_candidates(postgres_store):
    await setup_workspace(postgres_store)
    assert await postgres_store.workspace_repo.delete_workspace("ws1") == []
    assert await postgres_store.workspace_repo.delete_workspace("ws1") == []


async def test_migration_preserves_preexisting_workspace_files(postgres_store, test_rdb_config):
    import asyncio
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import URL, create_engine, text

    await setup_workspace(postgres_store)
    await upload(postgres_store, "legacy", workspace_ids=["ws1"])
    config = test_rdb_config

    def migrate_legacy_data():
        migration = importlib.import_module(
            "services.persistence.migrations.alembic.versions.c0d1e2f3a4b5_add_file_indexing_ownership"
        )
        engine = create_engine(
            URL.create(
                "postgresql",
                username=config.user,
                password=config.password,
                host=config.host,
                port=config.port,
                database=config.database,
            )
        )
        try:
            with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
                migration.downgrade()
                migration.upgrade()
                migration.upgrade()
                assert (
                    conn.execute(text("SELECT independently_indexed FROM files WHERE file_id = 'legacy'")).scalar()
                    is True
                )
        finally:
            engine.dispose()

    await asyncio.to_thread(migrate_legacy_data)
    assert await postgres_store.workspace_repo.delete_workspace("ws1") == []
    assert await postgres_store.document_repo.file_exists_in_partition("legacy", "p")
