"""Shared fixtures for the Phase 7F persistence-layer integration tests.

The whole suite auto-skips when a Postgres instance is not reachable. We try
the explicit ``POSTGRES_TEST_DSN`` env var first, then fall back to the local
docker-compose ``rdb`` container (the dev DB the rest of the project assumes
is up). One ephemeral test database is created at session start, Alembic
migrations run once, and individual tests share the same
:class:`PostgresStore`; an autouse fixture truncates user-modifiable tables
between tests so each one starts clean without paying for a full
drop/migrate cycle per case.

The repository integration tests live under ``tests/integration/repos/`` rather than
alongside the repository code because they need a real database — pytest's
``testpaths`` discovers them with the rest of the top-level test tree; invoke explicitly with
``uv run pytest tests/integration/repos``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

# tests/integration/repos is outside the pytest.ini `pythonpath = ./openrag`
# search path, so we have to add it ourselves before importing project
# modules. Mirrors how scripts/migrations/env.py wires its path.
_OPENRAG = Path(__file__).resolve().parents[3] / "openrag"
if str(_OPENRAG) not in sys.path:
    sys.path.insert(0, str(_OPENRAG))

from core.config.infrastructure import RDBConfig  # noqa: E402
from services.storage.postgres_store import PostgresStore  # noqa: E402

_DEFAULT_ADMIN_DSN = "postgresql://root:root_password@172.21.0.4:5432/postgres"
_TEST_DB_NAME = "openrag_phase7_test"


def _admin_dsn() -> str:
    return os.environ.get("POSTGRES_TEST_ADMIN_DSN", _DEFAULT_ADMIN_DSN)


def _admin_dsn_parts() -> dict[str, str | int]:
    """Decompose the admin DSN into the bits :class:`RDBConfig` needs."""
    import urllib.parse

    p = urllib.parse.urlparse(_admin_dsn())
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 5432,
        "user": p.username or "postgres",
        "password": p.password or "",
    }


async def _connect_admin() -> asyncpg.Connection | None:
    """Open an admin connection or return None when the server is unreachable."""
    try:
        return await asyncpg.connect(_admin_dsn(), timeout=5)
    except (OSError, asyncpg.PostgresError):
        return None


async def _drop_test_database(conn: asyncpg.Connection) -> None:
    # Kick any stale sessions off the test DB before dropping it. ``DROP
    # DATABASE`` fails on active connections — important when a previous
    # crashed session left a pool open.
    await conn.execute(
        """
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = $1 AND pid <> pg_backend_pid()
        """,
        _TEST_DB_NAME,
    )
    await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB_NAME}"')


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _ensured_test_database() -> str:
    """Create the test database from scratch; drop it at session teardown."""
    admin = await _connect_admin()
    if admin is None:
        pytest.skip(
            f"Postgres unreachable at {_admin_dsn()}; set POSTGRES_TEST_ADMIN_DSN or start the rdb container.",
        )
    try:
        await _drop_test_database(admin)
        await admin.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    finally:
        await admin.close()

    yield _TEST_DB_NAME

    admin = await _connect_admin()
    if admin is None:
        return
    try:
        await _drop_test_database(admin)
    finally:
        await admin.close()


@pytest.fixture(scope="session")
def test_rdb_config(_ensured_test_database: str) -> RDBConfig:
    """A :class:`RDBConfig` pointed at the ephemeral test database.

    Exposed as its own fixture so per-test lifecycle assertions can build
    their own short-lived :class:`PostgresStore` without disturbing the
    session-scoped pool.
    """
    parts = _admin_dsn_parts()
    return RDBConfig(
        host=str(parts["host"]),
        port=int(parts["port"]),
        user=str(parts["user"]),
        password=str(parts["password"]),
        database=_ensured_test_database,
        pool_min_size=1,
        pool_max_size=4,
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def postgres_store(test_rdb_config: RDBConfig) -> PostgresStore:
    """A real :class:`PostgresStore` against the freshly-created test DB.

    Migrations run once per session via :meth:`PostgresStore.initialize`.
    """
    store = PostgresStore(test_rdb_config, run_migrations=True)
    await store.initialize()
    try:
        yield store
    finally:
        await store.shutdown()


_TRUNCATE_SQL = """
TRUNCATE TABLE
    jobs,
    oidc_sessions,
    workspace_files,
    workspaces,
    partition_memberships,
    files,
    partitions,
    users
RESTART IDENTITY CASCADE
"""


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_db(postgres_store: PostgresStore):
    """Wipe user-modifiable tables before each test.

    ``alembic_version`` is left alone — migrations only run once per session.
    Identities are restarted so primary keys reset to 1, giving each test a
    deterministic ``users.id`` / ``files.id`` starting point.
    """
    async with postgres_store.pool.acquire() as conn:
        await conn.execute(_TRUNCATE_SQL)
    yield
