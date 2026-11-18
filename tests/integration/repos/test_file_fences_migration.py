"""Revision 934e9f4e871f against a real Postgres: the file_fences table (#1430)."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
import pytest_asyncio
from alembic import command

from .conftest import _admin_dsn, _connect_admin
from .test_users_managed_by_config_migration import _alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

REVISION = "934e9f4e871f"
PREVIOUS = "f3a4b5c6d7e8"


@pytest_asyncio.fixture(loop_scope="session")
async def previous_db():
    """An empty database migrated up to the revision before the one under test."""
    admin = await _connect_admin()
    if admin is None:
        pytest.skip("Postgres unreachable; set POSTGRES_TEST_ADMIN_DSN or start the rdb container.")
    name = f"openrag_fences_migration_{uuid.uuid4().hex[:8]}"
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()

    cfg = _alembic_config(name)
    await asyncio.to_thread(command.upgrade, cfg, PREVIOUS)
    conn = await asyncpg.connect(_admin_dsn().rsplit("/", 1)[0] + f"/{name}")
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


async def _has_table(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('public.file_fences') IS NOT NULL"))


async def test_upgrade_creates_the_table_and_downgrade_drops_it(previous_db):
    conn, cfg = previous_db
    assert not await _has_table(conn)

    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _has_table(conn)
    await conn.execute("INSERT INTO partitions (partition, created_at) VALUES ('p', now())")
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO file_fences (partition_name, file_id, holder_kind, holder_id, expires_at)"
            " VALUES ('p', 'f', 'other', 'h', now())"
        )

    await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
    assert not await _has_table(conn)

    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _has_table(conn)


async def test_upgrade_leaves_a_table_create_all_already_made(previous_db):
    conn, cfg = previous_db
    await conn.execute(
        """
        CREATE TABLE file_fences (
            partition_name VARCHAR NOT NULL REFERENCES partitions (partition) ON DELETE CASCADE,
            file_id VARCHAR NOT NULL,
            holder_kind VARCHAR NOT NULL,
            holder_id VARCHAR NOT NULL,
            acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (partition_name, file_id)
        )
        """
    )

    await asyncio.to_thread(command.upgrade, cfg, REVISION)

    assert await _has_table(conn)
