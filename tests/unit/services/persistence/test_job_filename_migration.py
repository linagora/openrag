"""The durable job filename column is safe on existing databases."""

from __future__ import annotations

import importlib
from pathlib import Path

import sqlalchemy as sa


def _migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.d5e6f7a8b9c0_add_job_filename",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.added: list[tuple[str, sa.Column]] = []
        self.dropped: list[tuple[str, str]] = []

    def add_column(self, table, column) -> None:
        self.added.append((table, column))

    def drop_column(self, table, column) -> None:
        self.dropped.append((table, column))


def test_migration_has_the_current_jobs_head(monkeypatch):
    migration = _migration(monkeypatch)

    assert migration.revision == "d5e6f7a8b9c0"
    assert migration.down_revision == "c4e8f2a6b913"


def test_upgrade_adds_filename_once(monkeypatch):
    migration = _migration(monkeypatch)
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "column_exists", lambda _table, column: column == "id")

    migration.upgrade()

    assert len(op.added) == 1
    table, column = op.added[0]
    assert table == "jobs"
    assert column.name == "filename"
    assert column.nullable is True


def test_upgrade_is_a_no_op_when_filename_exists(monkeypatch):
    migration = _migration(monkeypatch)
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "column_exists", lambda _table, _column: True)

    migration.upgrade()

    assert op.added == []


def test_downgrade_removes_filename(monkeypatch):
    migration = _migration(monkeypatch)
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "column_exists", lambda _table, _column: True)

    migration.downgrade()

    assert op.dropped == [("jobs", "filename")]
