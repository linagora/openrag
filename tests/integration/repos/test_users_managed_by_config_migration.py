"""Revision 5e6d7c8b9a01 against a real Postgres: users.managed_by_config."""

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

REVISION = "5e6d7c8b9a01"
PREVIOUS = "0794ddd13291"


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
async def previous_db():
    """An empty database migrated up to the revision before the one under test."""
    admin = await _connect_admin()
    if admin is None:
        pytest.skip("Postgres unreachable; set POSTGRES_TEST_ADMIN_DSN or start the rdb container.")
    name = f"openrag_managed_migration_{uuid.uuid4().hex[:8]}"
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


async def _has_column(conn: asyncpg.Connection) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'managed_by_config'"
        )
    )


async def test_upgrade_marks_existing_users_unmanaged_and_downgrade_drops_the_column(previous_db):
    conn, cfg = previous_db
    await conn.execute(
        "INSERT INTO users (id, display_name, is_admin, file_count, created_at) VALUES (7, 'x', FALSE, 0, NOW())"
    )
    assert not await _has_column(conn)

    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await conn.fetchval("SELECT managed_by_config FROM users WHERE id = 7") is False

    await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
    assert not await _has_column(conn)

    # Idempotent: the column already present (create_all ran first) is not re-added.
    await conn.execute("ALTER TABLE users ADD COLUMN managed_by_config BOOLEAN NOT NULL DEFAULT FALSE")
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _has_column(conn)
