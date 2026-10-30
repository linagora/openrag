"""Revision a2b3c4d5e6f7 against a real Postgres: file_quota_reservations."""

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

REVISION = "a2b3c4d5e6f7"
PREVIOUS = "bcbdf503a6f1"


@pytest_asyncio.fixture(loop_scope="session")
async def previous_db():
    """An empty database migrated up to the revision before the one under test."""
    admin = await _connect_admin()
    if admin is None:
        pytest.skip("Postgres unreachable; set POSTGRES_TEST_ADMIN_DSN or start the rdb container.")
    name = f"openrag_reservations_migration_{uuid.uuid4().hex[:8]}"
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
    return bool(await conn.fetchval("SELECT to_regclass('file_quota_reservations') IS NOT NULL"))


async def test_upgrade_keeps_existing_counts_and_downgrade_gives_held_slots_back(previous_db):
    conn, cfg = previous_db
    await conn.execute(
        "INSERT INTO users (id, display_name, is_admin, file_count, file_quota, created_at)"
        " VALUES (7, 'x', FALSE, 3, 5, NOW())"
    )

    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _has_table(conn)
    assert await conn.fetchval("SELECT file_count FROM users WHERE id = 7") == 3

    # Two uploads in flight hold two more slots.
    await conn.execute("UPDATE users SET file_count = file_count + 2 WHERE id = 7")
    await conn.execute("INSERT INTO file_quota_reservations (id, user_id) VALUES ('a', 7), ('b', 7)")

    await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
    assert not await _has_table(conn)
    assert await conn.fetchval("SELECT file_count FROM users WHERE id = 7") == 3

    # Idempotent: a table already created by create_all is not re-created.
    await conn.execute(
        "CREATE TABLE file_quota_reservations ("
        " id VARCHAR PRIMARY KEY,"
        " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await conn.fetchval("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_file_quota_reservations_user_id'")
