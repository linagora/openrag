"""Revision a7b8c9d0e1f2 against a real Postgres: workspace_id unique per partition.

The session-wide ``postgres_store`` fixture migrates an empty database
straight to ``head``, which never exercises the interesting path: a
populated database on the previous revision, where ``workspace_files``
still joins on the string ``workspace_id``. This module builds its own
database, stops the migration chain one step short, seeds legacy rows,
then upgrades and downgrades through the revision under test.
"""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from services.persistence.connection import _ALEMBIC_INI, _MIGRATIONS_DIR
from sqlalchemy import URL

from .conftest import _admin_dsn, _admin_dsn_parts, _connect_admin

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

REVISION = "a7b8c9d0e1f2"
PREVIOUS = "d5e6f7a8b9c0"


def _alembic_config(database: str) -> Config:
    parts = _admin_dsn_parts()
    url = URL.create(
        "postgresql",
        username=str(parts["user"]),
        password=str(parts["password"]),
        host=str(parts["host"]),
        port=int(parts["port"]),
        database=database,
    ).render_as_string(hide_password=False)
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest_asyncio.fixture(loop_scope="session")
async def legacy_db():
    """An empty database migrated up to the revision *before* the one under test."""
    admin = await _connect_admin()
    if admin is None:
        pytest.skip("Postgres unreachable; set POSTGRES_TEST_ADMIN_DSN or start the rdb container.")
    name = f"openrag_ws_migration_{uuid.uuid4().hex[:8]}"
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()

    cfg = _alembic_config(name)
    await asyncio.to_thread(command.upgrade, cfg, PREVIOUS)
    dsn = _admin_dsn().rsplit("/", 1)[0] + f"/{name}"
    conn = await asyncpg.connect(dsn)
    try:
        yield conn, cfg
    finally:
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


async def _seed_legacy(conn: asyncpg.Connection) -> dict[str, int]:
    """Two partitions, one workspace each, one file attached to each workspace."""
    await conn.execute("INSERT INTO partitions (partition, created_at) VALUES ('a', NOW()), ('b', NOW())")
    await conn.execute(
        """
        INSERT INTO files (file_id, partition_name, file_metadata)
        VALUES ('doc', 'a', '{}'), ('doc', 'b', '{}')
        """
    )
    await conn.execute(
        """
        INSERT INTO workspaces (workspace_id, partition_name, display_name)
        VALUES ('ws-a', 'a', 'A'), ('ws-b', 'b', 'B')
        """
    )
    file_pks = {
        r["partition_name"]: r["id"] for r in await conn.fetch("SELECT id, partition_name FROM files ORDER BY id")
    }
    await conn.execute(
        "INSERT INTO workspace_files (workspace_id, file_id) VALUES ('ws-a', $1), ('ws-b', $2)",
        file_pks["a"],
        file_pks["b"],
    )
    return file_pks


async def _column_type(conn: asyncpg.Connection, table: str, column: str) -> str:
    return await conn.fetchval(
        "SELECT data_type FROM information_schema.columns WHERE table_name = $1 AND column_name = $2",
        table,
        column,
    )


async def _join_rows(conn: asyncpg.Connection) -> set[tuple[str, str, str]]:
    """(partition, workspace_id, file_id) for every workspace_files row, via the current join key."""
    rows = await conn.fetch(
        """
        SELECT w.partition_name, w.workspace_id, f.file_id
        FROM workspace_files wf
        JOIN workspaces w ON w.id = wf.workspace_id
        JOIN files f ON f.id = wf.file_id
        """
    )
    return {(r["partition_name"], r["workspace_id"], r["file_id"]) for r in rows}


async def test_legacy_shape_is_what_the_migration_expects(legacy_db):
    conn, _ = legacy_db
    assert await _column_type(conn, "workspace_files", "workspace_id") == "character varying"
    await _seed_legacy(conn)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute("INSERT INTO workspaces (workspace_id, partition_name) VALUES ('ws-a', 'b')")


async def test_upgrade_rekeys_the_join_and_allows_the_same_id_per_partition(legacy_db):
    conn, cfg = legacy_db
    await _seed_legacy(conn)

    await asyncio.to_thread(command.upgrade, cfg, REVISION)

    assert await _column_type(conn, "workspace_files", "workspace_id") == "integer"
    # Every membership survived and points at the right workspace.
    assert await _join_rows(conn) == {("a", "ws-a", "doc"), ("b", "ws-b", "doc")}

    # The same id is now allowed in another partition, still not twice in one.
    await conn.execute("INSERT INTO workspaces (workspace_id, partition_name) VALUES ('ws-a', 'b')")
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute("INSERT INTO workspaces (workspace_id, partition_name) VALUES ('ws-a', 'a')")

    # Deleting a workspace still cascades to its memberships through the new FK.
    await conn.execute("DELETE FROM workspaces WHERE partition_name = 'a' AND workspace_id = 'ws-a'")
    assert await _join_rows(conn) == {("b", "ws-b", "doc")}

    # Re-running is a no-op.
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _column_type(conn, "workspace_files", "workspace_id") == "integer"


async def test_downgrade_refuses_shared_ids_then_restores_the_string_join(legacy_db):
    conn, cfg = legacy_db
    await _seed_legacy(conn)
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    await conn.execute("INSERT INTO workspaces (workspace_id, partition_name) VALUES ('ws-a', 'b')")

    with pytest.raises(Exception, match="ws-a"):
        await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
    # Nothing changed: the check runs before any schema op.
    assert await _column_type(conn, "workspace_files", "workspace_id") == "integer"

    await conn.execute("DELETE FROM workspaces WHERE partition_name = 'b' AND workspace_id = 'ws-a'")
    await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)

    assert await _column_type(conn, "workspace_files", "workspace_id") == "character varying"
    rows = await conn.fetch(
        """
        SELECT wf.workspace_id, f.file_id, f.partition_name
        FROM workspace_files wf JOIN files f ON f.id = wf.file_id
        """
    )
    assert {(r["workspace_id"], r["file_id"], r["partition_name"]) for r in rows} == {
        ("ws-a", "doc", "a"),
        ("ws-b", "doc", "b"),
    }
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute("INSERT INTO workspaces (workspace_id, partition_name) VALUES ('ws-a', 'b')")
