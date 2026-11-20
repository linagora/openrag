"""The source-index migration is guarded both ways, so it re-runs after create_all."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.9ed228e7ea17_index_files_source",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.dropped: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def drop_index(self, name: str, table_name: str) -> None:
        self.dropped.append(f"{table_name}.{name}")


def _install(monkeypatch, migration, *, exists: bool) -> _FakeOp:
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "index_exists", lambda table, index: exists)
    return op


def test_chains_after_the_file_fences_revision(migration):
    assert migration.down_revision == "a2b3c4d5e6f7"


def test_upgrade_creates_the_expression_index(monkeypatch, migration):
    op = _install(monkeypatch, migration, exists=False)
    migration.upgrade()
    assert op.executed == ["CREATE INDEX ix_files_source ON files ((file_metadata ->> 'source'))"]


def test_upgrade_skips_an_index_create_all_made(monkeypatch, migration):
    op = _install(monkeypatch, migration, exists=True)
    migration.upgrade()
    assert op.executed == []


def test_downgrade_drops_only_an_existing_index(monkeypatch, migration):
    op = _install(monkeypatch, migration, exists=True)
    migration.downgrade()
    assert op.dropped == ["files.ix_files_source"]
    op = _install(monkeypatch, migration, exists=False)
    migration.downgrade()
    assert op.dropped == []


def test_the_schema_declares_the_same_index():
    from services.persistence.schema import files
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex

    index = next(i for i in files.indexes if i.name == "ix_files_source")
    assert str(CreateIndex(index).compile(dialect=postgresql.dialect())) == (
        "CREATE INDEX ix_files_source ON files ((file_metadata ->> 'source'))"
    )
