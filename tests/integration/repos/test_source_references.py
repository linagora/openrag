"""Which uploaded originals the catalog still references, and the index behind it."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from services.storage.postgres_store import PostgresStore

from .conftest import _admin_dsn, _connect_admin
from .test_users_managed_by_config_migration import _alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

REVISION = "9ed228e7ea17"
PREVIOUS = "934e9f4e871f"


async def _add(store: PostgresStore, file_id: str, partition: str, source: str | None) -> None:
    metadata = {"filename": f"{file_id}.pdf"}
    if source is not None:
        metadata["source"] = source
    assert await store.document_repo.add_file_to_partition(file_id=file_id, partition=partition, file_metadata=metadata)


async def test_referenced_sources_names_only_paths_a_row_holds(postgres_store):
    for partition in ("a", "b"):
        await postgres_store.partition_repo.create_partition(partition)
    await _add(postgres_store, "f1", "a", "/data/1_report.pdf")
    # A copy keeps its source's ``source``, here in another partition.
    await _add(postgres_store, "f1-copy", "b", "/data/1_report.pdf")
    await _add(postgres_store, "f2", "a", "/data/2_notes.md")
    await _add(postgres_store, "f3", "a", None)

    repo = postgres_store.document_repo
    found = await repo.referenced_sources(["/data/1_report.pdf", "/data/2_notes.md", "/data/gone.pdf", ""])
    assert found == {"/data/1_report.pdf", "/data/2_notes.md"}
    assert await repo.referenced_sources([]) == set()

    await repo.remove_file_from_partition("f1", "a")
    assert await repo.referenced_sources(["/data/1_report.pdf"]) == {"/data/1_report.pdf"}
    await repo.remove_file_from_partition("f1-copy", "b")
    assert await repo.referenced_sources(["/data/1_report.pdf"]) == set()


async def test_list_partition_sources(postgres_store):
    for partition in ("a", "b"):
        await postgres_store.partition_repo.create_partition(partition)
    await _add(postgres_store, "f1", "a", "/data/1_report.pdf")
    await _add(postgres_store, "f1-copy", "a", "/data/1_report.pdf")
    await _add(postgres_store, "f2", "a", "/data/2_notes.md")
    await _add(postgres_store, "f3", "a", None)
    await _add(postgres_store, "g1", "b", "/data/3_other.pdf")

    sources = await postgres_store.document_repo.list_partition_sources("a")
    assert sorted(sources) == ["/data/1_report.pdf", "/data/2_notes.md"]
    assert await postgres_store.document_repo.list_partition_sources("missing") == []


async def test_the_lookup_uses_the_source_index(postgres_store):
    await postgres_store.partition_repo.create_partition("a")
    await _add(postgres_store, "f1", "a", "/data/1_report.pdf")
    async with postgres_store.document_repo.pool.acquire() as conn:
        async with conn.transaction():
            # The table is tiny, so tell the planner to avoid a sequential scan:
            # the plan then shows whether the query matches the index expression.
            await conn.execute("SET LOCAL enable_seqscan = off")
            plan = await conn.fetch(
                "EXPLAIN SELECT DISTINCT file_metadata ->> 'source' AS source FROM files "
                "WHERE file_metadata ->> 'source' = ANY($1::text[])",
                ["/data/1_report.pdf"],
            )
    assert "ix_files_source" in "\n".join(row[0] for row in plan)


# --- the migration ------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def previous_db():
    """An empty database migrated up to the revision before the one under test."""
    admin = await _connect_admin()
    if admin is None:
        pytest.skip("Postgres unreachable; set POSTGRES_TEST_ADMIN_DSN or start the rdb container.")
    name = f"openrag_source_index_{uuid.uuid4().hex[:8]}"
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()

    conn = None
    try:
        cfg = _alembic_config(name)
        await asyncio.to_thread(command.upgrade, cfg, PREVIOUS)
        conn = await asyncpg.connect(_admin_dsn().rsplit("/", 1)[0] + f"/{name}")
        yield conn, cfg
    finally:
        if conn is not None:
            await conn.close()
        admin = await _connect_admin()
        if admin is not None:
            try:
                await admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
                    name,
                )
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
            finally:
                await admin.close()


async def _has_index(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('public.ix_files_source') IS NOT NULL"))


async def test_upgrade_creates_the_index_and_downgrade_drops_it(previous_db):
    conn, cfg = previous_db
    assert not await _has_index(conn)
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _has_index(conn)
    await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
    assert not await _has_index(conn)
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _has_index(conn)


async def test_upgrade_leaves_an_index_create_all_already_made(previous_db):
    conn, cfg = previous_db
    await conn.execute("CREATE INDEX ix_files_source ON files ((file_metadata ->> 'source'))")
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _has_index(conn)
