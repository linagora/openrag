from __future__ import annotations

from core.config.infrastructure import RDBConfig, VectorDBConfig
from core.config.root import Settings


def test_rdb_config_for_migrations_derives_database_from_collection() -> None:
    from services.persistence.migrations.run import _rdb_config_for_migrations

    settings = Settings(
        rdb=RDBConfig(password="x", database=None),
        vectordb=VectorDBConfig(collection_name="managed"),
    )

    rdb = _rdb_config_for_migrations(settings)

    assert rdb.database == "partitions_for_collection_managed"


def test_rdb_config_for_migrations_preserves_explicit_database() -> None:
    from services.persistence.migrations.run import _rdb_config_for_migrations

    settings = Settings(
        rdb=RDBConfig(password="x", database="openrag_catalog"),
        vectordb=VectorDBConfig(collection_name="managed"),
    )

    rdb = _rdb_config_for_migrations(settings)

    assert rdb.database == "openrag_catalog"


def test_migration_entrypoint_runs_alembic_without_initializing_pool(monkeypatch) -> None:
    import services.persistence.migrations.run as migration_run

    calls: list[str] = []
    settings = Settings(
        rdb=RDBConfig(password="x", database="openrag_catalog"),
        vectordb=VectorDBConfig(collection_name="managed"),
    )

    class FakeConnectionManager:
        def __init__(self, rdb):
            assert rdb.database == "openrag_catalog"

        async def initialize(self):  # pragma: no cover - should never be called
            raise AssertionError("migration entrypoint must not open the application pool")

        async def run_migrations(self):
            calls.append("run_migrations")

    monkeypatch.setattr(migration_run, "load_config", lambda: settings)
    monkeypatch.setattr(migration_run, "ConnectionManager", FakeConnectionManager)

    assert migration_run.main() == 0
    assert calls == ["run_migrations"]
