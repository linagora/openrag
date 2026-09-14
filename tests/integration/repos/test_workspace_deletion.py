"""Workspace deletion through the production service, catalog writer, and SQL."""

import asyncio
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
        if workspace_ids:
            assert await store.document_repo.finalize_file_workspace_ownership(file_id, partition, workspace_ids)


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
    final_result = await svc.delete_workspace("p", "ws2")

    assert final_result["orphaned_files_deleted"] == (1 if shared else 0)
    assert await store.document_repo.file_exists_in_partition("kept", "p") is not shared
    if shared:
        vectors.delete.assert_awaited_once_with(["kept-chunk"], "test")
    else:
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


async def test_workspace_attachment_cannot_race_orphan_cleanup(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await store.workspace_repo.create_workspace(Workspace(workspace_id="ws2", partition="p"))
    await upload(store, "exclusive", workspace_ids=["ws1"])

    async with store.pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("LOCK TABLE workspaces IN SHARE MODE")

        deletion = asyncio.create_task(store.workspace_repo.delete_workspace("ws1"))
        for _ in range(100):
            blocked = await store.pool.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND wait_event_type = 'Lock'
                      AND query LIKE '%DELETE FROM workspaces%'
                )
                """,
            )
            if blocked:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("workspace deletion did not reach the blocked DELETE")

        attachment = asyncio.create_task(
            store.workspace_repo.add_files_to_workspace("ws2", ["exclusive"]),
        )
        await asyncio.sleep(0.05)
        assert not attachment.done()

        await tx.commit()

    assert await deletion == ["exclusive"]
    assert await attachment == ["exclusive"]
    assert await store.document_repo.file_exists_in_partition("exclusive", "p")


async def test_workspace_cleanup_rechecks_membership_after_waiting_for_attachment(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await store.workspace_repo.create_workspace(Workspace(workspace_id="ws2", partition="p"))
    await upload(store, "exclusive", workspace_ids=["ws1"])

    async with store.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("LOCK TABLE workspace_files IN SHARE MODE")

            attachment = asyncio.create_task(
                store.workspace_repo.add_files_to_workspace("ws2", ["exclusive"]),
            )
            for _ in range(100):
                blocked = await store.pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND wait_event_type = 'Lock'
                          AND query LIKE '%INSERT INTO workspace_files%'
                    )
                    """,
                )
                if blocked:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("workspace attachment did not reach the blocked INSERT")

            deletion = asyncio.create_task(store.workspace_repo.delete_workspace("ws1"))
            await asyncio.sleep(0.05)
            assert not deletion.done()

    assert await attachment == []
    assert await deletion == []
    assert await store.workspace_repo.list_workspace_files("ws2") == ["exclusive"]
    assert await store.document_repo.file_exists_in_partition("exclusive", "p")


async def test_concurrent_last_workspace_deletions_claim_the_shared_file(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await store.workspace_repo.create_workspace(Workspace(workspace_id="ws2", partition="p"))
    await upload(store, "shared", workspace_ids=["ws1", "ws2"])

    async with store.pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.fetch(
            """
            SELECT id
            FROM files
            WHERE file_id = 'shared'
            FOR UPDATE
            """,
        )

        delete_ws1 = asyncio.create_task(store.workspace_repo.delete_workspace("ws1"))
        delete_ws2 = asyncio.create_task(store.workspace_repo.delete_workspace("ws2"))
        await asyncio.sleep(0.05)
        assert not delete_ws1.done()
        assert not delete_ws2.done()

        await tx.commit()

    orphan_lists = await asyncio.gather(delete_ws1, delete_ws2)
    assert [file_id for orphans in orphan_lists for file_id in orphans] == ["shared"]


async def test_stale_cleanup_claim_can_be_attached_again(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await upload(store, "stale", workspace_ids=["ws1"])
    await store.pool.execute(
        """
        UPDATE files
        SET workspace_cleanup_claimed = TRUE,
            workspace_cleanup_claimed_at = NOW() - INTERVAL '2 hours',
            workspace_cleanup_state = 'CLAIMED'
        WHERE file_id = 'stale'
        """,
    )

    assert await store.workspace_repo.add_files_to_workspace("ws1", ["stale"]) == []
    assert (
        await store.pool.fetchval(
            "SELECT workspace_cleanup_claimed FROM files WHERE file_id = 'stale'",
        )
        is False
    )


async def test_stale_destructive_cleanup_claim_cannot_be_attached(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await upload(store, "stale", workspace_ids=["ws1"])
    await store.pool.execute(
        """
        UPDATE files
        SET workspace_cleanup_claimed = TRUE,
            workspace_cleanup_claimed_at = NOW() - INTERVAL '2 hours',
            workspace_cleanup_started = TRUE,
            workspace_cleanup_state = 'CLEANUP_STARTED'
        WHERE file_id = 'stale'
        """,
    )

    assert await store.workspace_repo.add_files_to_workspace("ws1", ["stale"]) == ["stale"]
    assert (
        await store.pool.fetchval(
            "SELECT workspace_cleanup_claimed FROM files WHERE file_id = 'stale'",
        )
        is True
    )


async def test_failed_vector_cleanup_is_durable_and_retryable(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await upload(store, "retryable", workspace_ids=["ws1"])
    svc, vectors = service(store)
    vectors.delete.side_effect = RuntimeError("temporary vector-store failure")

    result = await svc.delete_workspace("p", "ws1")
    assert result["orphaned_files_failed"] == ["retryable"]
    state = await store.pool.fetchrow(
        """
        SELECT workspace_cleanup_claimed, workspace_cleanup_started, workspace_cleanup_failed,
               workspace_cleanup_state
        FROM files WHERE file_id = 'retryable'
        """,
    )
    assert dict(state) == {
        "workspace_cleanup_claimed": True,
        "workspace_cleanup_started": True,
        "workspace_cleanup_failed": True,
        "workspace_cleanup_state": "CLEANUP_FAILED",
    }
    await store.workspace_repo.create_workspace(Workspace(workspace_id="ws2", partition="p"))
    assert await store.workspace_repo.add_files_to_workspace("ws2", ["retryable"]) == ["retryable"]

    vectors.delete.side_effect = None
    vectors.query_ids_by_filter.side_effect = RuntimeError("retry query failed")
    with pytest.raises(RuntimeError, match="retry query failed"):
        await svc.retry_failed_file_cleanup("retryable", "p")
    assert (
        await store.pool.fetchval("SELECT workspace_cleanup_state FROM files WHERE file_id = 'retryable'")
        == "CLEANUP_FAILED"
    )
    assert await store.workspace_repo.add_files_to_workspace("ws2", ["retryable"]) == ["retryable"]
    vectors.query_ids_by_filter.side_effect = None
    # Retry already enters CLEANUP_STARTED and must not start a second time.
    assert await svc.retry_failed_file_cleanup("retryable", "p") is True
    assert not await store.document_repo.file_exists_in_partition("retryable", "p")


async def test_concurrent_cleanup_retries_only_one_worker_claims_file(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await upload(store, "retryable", workspace_ids=["ws1"])
    await store.pool.execute(
        """
        UPDATE files
        SET workspace_cleanup_claimed = TRUE,
            workspace_cleanup_started = TRUE,
            workspace_cleanup_failed = TRUE,
            workspace_cleanup_claimed_at = NOW(),
            workspace_cleanup_state = 'CLEANUP_FAILED'
        WHERE file_id = 'retryable'
        """,
    )

    async def claim():
        async with store.workspace_repo.cleanup_session("retryable", "p") as owned:
            return owned is not None and await owned.claim_failed_file_cleanup("retryable", "p")

    assert await asyncio.gather(claim(), claim()) in ([True, False], [False, True])


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


async def test_active_cleanup_cannot_be_reclaimed_even_when_timestamp_expires(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await upload(store, "slow", workspace_ids=["ws1"])
    svc, vectors = service(store)
    deleting = asyncio.Event()
    finish = asyncio.Event()

    async def slow_delete(*args):
        deleting.set()
        await finish.wait()

    vectors.delete.side_effect = slow_delete
    task = asyncio.create_task(svc.delete_workspace("p", "ws1"))
    try:
        await asyncio.wait_for(deleting.wait(), timeout=5)
        await store.pool.execute(
            "UPDATE files SET workspace_cleanup_claimed_at = NOW() - INTERVAL '2 hours' WHERE file_id = 'slow'"
        )
        assert await svc.retry_failed_file_cleanup("slow", "p") is False
        async with store.workspace_repo.cleanup_session("slow", "p") as competing:
            assert competing is None
        assert vectors.delete.await_count == 1
    finally:
        finish.set()
        result = await task
    assert result["orphaned_files_deleted"] == 1


async def test_expired_session_cannot_mutate_new_cleanup_owner(postgres_store):
    repo = postgres_store.workspace_repo
    async with repo.cleanup_session("f", "p") as old:
        assert old is not None
    async with repo.cleanup_session("f", "p") as current:
        assert current is not None
        for transition in (old.mark_cleanup_failed, old.finalize_claimed_file_cleanup, old.start_claimed_file_cleanup):
            with pytest.raises(RuntimeError, match="active owning session"):
                await transition("f", "p")


async def test_pending_attachments_protect_upload_during_workspace_deletion(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    # Catalog creation precedes the gather of attachment operations.
    assert await _write_catalog_record(
        doc_repo=store.document_repo,
        metadata={"file_id": "pending"},
        partition="p",
        user=None,
        replace=False,
        indexation_config=None,
        workspace_ids=["ws1", "missing"],
    )
    assert await store.workspace_repo.add_files_to_workspace("ws1", ["pending"]) == []
    svc, vectors = service(store)
    assert (await svc.delete_workspace("p", "ws1"))["orphaned_files_deleted"] == 0
    assert await store.workspace_repo.add_files_to_workspace("missing", ["pending"]) == ["pending"]
    assert await store.document_repo.mark_file_independently_indexed("pending", "p")
    assert not await store.document_repo.finalize_file_workspace_ownership("pending", "p", ["ws1", "missing"])
    assert await store.document_repo.file_exists_in_partition("pending", "p")
    vectors.delete.assert_not_awaited()


async def test_protection_cannot_claim_success_after_destructive_cleanup_starts(postgres_store):
    store = postgres_store
    await setup_workspace(store)
    await upload(store, "f", workspace_ids=["ws1"])
    assert await store.workspace_repo.delete_workspace("ws1") == ["f"]
    async with store.workspace_repo.cleanup_session("f", "p") as owned:
        assert await owned.start_claimed_file_cleanup("f", "p")
        assert not await store.document_repo.mark_file_independently_indexed("f", "p")
        assert (
            await store.pool.fetchval("SELECT workspace_cleanup_state FROM files WHERE file_id = 'f'")
            == "CLEANUP_STARTED"
        )


async def test_cleanup_state_migration_repairs_missing_constraint_and_downgrades(postgres_store, test_rdb_config):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import URL, create_engine, inspect, text

    config = test_rdb_config
    await setup_workspace(postgres_store)
    await upload(postgres_store, "f", workspace_ids=["ws1"])

    def migrate():
        migration = importlib.import_module(
            "services.persistence.migrations.alembic.versions.f5a6b7c8d9e0_add_workspace_cleanup_state"
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
                constraint = "ck_files_workspace_cleanup_state"
                conn.execute(text(f"ALTER TABLE files DROP CONSTRAINT {constraint}"))
                migration.upgrade()
                migration.upgrade()
                assert constraint in {c["name"] for c in inspect(conn).get_check_constraints("files")}
                conn.execute(text(f"ALTER TABLE files DROP CONSTRAINT {constraint}"))
                migration.downgrade()
                migration.downgrade()
                migration.upgrade()
                assert constraint in {c["name"] for c in inspect(conn).get_check_constraints("files")}
        finally:
            engine.dispose()

    await asyncio.to_thread(migrate)
