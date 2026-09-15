"""Revision e1f3a5c7d9b2 — partition_embedder_swaps (#762 F4)."""

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
        "services.persistence.migrations.alembic.versions.e1f3a5c7d9b2_add_partition_embedder_swaps",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_table(self, name, *columns):
        self.calls.append(("create_table", name, tuple(getattr(c, "name", None) for c in columns)))

    def create_index(self, name, table, columns):
        self.calls.append(("create_index", name, table, tuple(columns)))

    def drop_index(self, name, table_name):
        self.calls.append(("drop_index", name, table_name))

    def drop_table(self, name):
        self.calls.append(("drop_table", name))


def test_it_follows_the_embedder_vector_field_backfill(migration):
    assert migration.down_revision == "c4e8f2a6b913"


def test_upgrade_creates_the_table_and_its_indexes(monkeypatch, migration):
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _t: False)
    monkeypatch.setattr(migration, "index_exists", lambda _t, _i: False)

    migration.upgrade()

    kinds = [call[0] for call in op.calls]
    assert kinds == ["create_table", "create_index", "create_index"]
    assert op.calls[0][1] == "partition_embedder_swaps"
    assert {"partition", "target_embedder", "status", "files_done"} <= set(op.calls[0][2])


def test_upgrade_is_a_no_op_on_a_database_create_all_already_built(monkeypatch, migration):
    """create_all() runs before alembic at startup (see CLAUDE.md)."""
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _t: True)
    monkeypatch.setattr(migration, "index_exists", lambda _t, _i: True)

    migration.upgrade()

    assert op.calls == []


def test_downgrade_drops_only_what_exists(monkeypatch, migration):
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _t: False)
    monkeypatch.setattr(migration, "index_exists", lambda _t, _i: False)

    migration.downgrade()

    assert op.calls == []
