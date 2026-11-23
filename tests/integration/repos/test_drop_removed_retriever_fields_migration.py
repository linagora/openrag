"""Revision d9e2e59a7a4e against a real Postgres: which preset fields it removes."""

from __future__ import annotations

import asyncio
import json
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

REVISION = "d9e2e59a7a4e"
PREVIOUS = "a2b3c4d5e6f7"


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
    name = f"openrag_retriever_fields_migration_{uuid.uuid4().hex[:8]}"
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


# (name, preset_type, config before the upgrade, config expected after it)
PRESETS = [
    (
        "default",
        "retrieval",
        {"type": "single", "top_k": 50, "similarity_threshold": 0.6},
        {"top_k": 50, "similarity_threshold": 0.6},
    ),
    (
        "hyde",
        "retrieval",
        {"type": "hyde", "llm": "rewriter", "hyde_prompt_name": "h", "multi_query_prompt_name": "m", "top_k": 50},
        {"top_k": 50},
    ),
    ("clean", "retrieval", {"top_k": 20, "top_n": 5}, {"top_k": 20, "top_n": 5}),
    # An indexation preset is left alone, even with a key of the same name.
    (
        "default",
        "indexation",
        {"chunking": {"name": "recursive_splitter"}, "llm": "x"},
        {"chunking": {"name": "recursive_splitter"}, "llm": "x"},
    ),
]


async def _presets(conn: asyncpg.Connection) -> dict[tuple[str, str], dict]:
    rows = await conn.fetch("SELECT name, preset_type, config FROM pipeline_presets")
    return {(r["name"], r["preset_type"]): json.loads(r["config"]) for r in rows}


async def test_upgrade_drops_the_removed_fields_from_retrieval_presets(previous_db):
    conn, cfg = previous_db
    for name, preset_type, config, _ in PRESETS:
        await conn.execute(
            "INSERT INTO pipeline_presets (name, preset_type, config) VALUES ($1, $2, $3::jsonb)",
            name,
            preset_type,
            json.dumps(config),
        )

    await asyncio.to_thread(command.upgrade, cfg, REVISION)

    after = await _presets(conn)
    assert after == {(name, preset_type): expected for name, preset_type, _, expected in PRESETS}

    # The downgrade restores nothing, and upgrading again changes nothing.
    await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
    assert await _presets(conn) == after
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    assert await _presets(conn) == after
