from __future__ import annotations

from core.config.infrastructure import RDBConfig, VectorDBConfig
from core.config.root import Settings

# The derivation itself is covered by tests/unit/core/config/test_resolved_rdb.py.
# What this file pins is that the standalone entrypoint goes *through* it: run
# against a different database than the API opens, and the migrations silently
# upgrade an empty one.


def _connection_manager_arg(monkeypatch, settings: Settings) -> RDBConfig:
    """Drive ``main()`` with a stub manager, returning the RDBConfig it was handed."""
    import services.persistence.migrations.run as migration_run

    received: list[RDBConfig] = []
    calls: list[str] = []

    class FakeConnectionManager:
        def __init__(self, rdb):
            received.append(rdb)

        async def initialize(self):  # pragma: no cover - should never be called
            raise AssertionError("migration entrypoint must not open the application pool")

        async def run_migrations(self):
            calls.append("run_migrations")

    monkeypatch.setattr(migration_run, "load_config", lambda: settings)
    monkeypatch.setattr(migration_run, "ConnectionManager", FakeConnectionManager)

    assert migration_run.main() == 0
    assert calls == ["run_migrations"]
    return received[0]


def _settings(database: str | None) -> Settings:
    return Settings(
        rdb=RDBConfig(password="x", database=database),
        vectordb=VectorDBConfig(collection_name="managed"),
    )


def test_migration_entrypoint_derives_database_from_collection(monkeypatch) -> None:
    assert _connection_manager_arg(monkeypatch, _settings(None)).database == "partitions_for_collection_managed"


def test_migration_entrypoint_preserves_explicit_database(monkeypatch) -> None:
    assert _connection_manager_arg(monkeypatch, _settings("openrag_catalog")).database == "openrag_catalog"


def test_migration_entrypoint_runs_alembic_without_initializing_pool(monkeypatch) -> None:
    """The stub's ``initialize`` raises, so reaching it fails the test."""
    _connection_manager_arg(monkeypatch, _settings("openrag_catalog"))
