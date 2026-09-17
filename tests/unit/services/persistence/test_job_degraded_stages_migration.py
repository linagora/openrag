"""The job degradation migration is additive and safe to re-run."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    module_name = "services.persistence.migrations.alembic.versions.2a3b4c5d6e7f_add_job_degraded_stages"
    if importlib.util.find_spec(module_name) is None:
        return None
    return importlib.import_module(module_name)


class _FakeOp:
    def __init__(self) -> None:
        self.added: list[tuple[str, object]] = []
        self.dropped: list[tuple[str, str]] = []

    def add_column(self, table: str, column: object) -> None:
        self.added.append((table, column))

    def drop_column(self, table: str, column: str) -> None:
        self.dropped.append((table, column))


def test_upgrade_adds_the_column_once(monkeypatch, migration) -> None:
    assert migration is not None
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda table: table == "jobs")
    monkeypatch.setattr(migration, "column_exists", lambda _table, _column: False)

    migration.upgrade()

    assert len(op.added) == 1
    table, column = op.added[0]
    assert table == "jobs"
    assert column.name == "degraded_stages"
    assert column.nullable is False
    assert str(column.server_default.arg) == "ARRAY[]::text[]"


def test_migration_follows_chunk_count_migration(migration) -> None:
    assert migration is not None

    assert migration.down_revision == "09f6c4b8a2d1"


def test_upgrade_is_a_no_op_when_the_column_exists(monkeypatch, migration) -> None:
    assert migration is not None
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "column_exists", lambda _table, _column: True)

    migration.upgrade()

    assert op.added == []


def test_downgrade_removes_the_column_when_present(monkeypatch, migration) -> None:
    assert migration is not None
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "column_exists", lambda _table, _column: True)

    migration.downgrade()

    assert op.dropped == [("jobs", "degraded_stages")]
