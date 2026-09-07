"""The jobs migration must be re-runnable on a database that already has it."""

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
        "services.persistence.migrations.alembic.versions.c7d8e9f0a1b2_add_jobs",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.created_tables: list[str] = []
        self.created_indexes: list[str] = []

    def create_table(self, name, *_columns) -> None:
        self.created_tables.append(name)

    def create_index(self, name, table, columns) -> None:
        self.created_indexes.append(name)


def _install(monkeypatch, migration, *, exists: bool):
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: exists)
    monkeypatch.setattr(migration, "index_exists", lambda _table, _index: exists)
    return op


def test_migration_follows_the_previous_head(migration):
    assert migration.revision == "c7d8e9f0a1b2"
    assert migration.down_revision == "b9c0d1e2f3a4"


def test_upgrade_creates_the_table_and_its_indexes(monkeypatch, migration):
    op = _install(monkeypatch, migration, exists=False)

    migration.upgrade()

    assert op.created_tables == ["jobs"]
    assert op.created_indexes == [
        "ix_jobs_status",
        "ix_jobs_user_id",
        "ix_jobs_partition_file_id",
    ]


def test_upgrade_is_a_no_op_when_the_table_is_already_there(monkeypatch, migration):
    op = _install(monkeypatch, migration, exists=True)

    migration.upgrade()

    assert op.created_tables == []
    assert op.created_indexes == []
