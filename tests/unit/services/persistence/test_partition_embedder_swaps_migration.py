"""Revisions e1f3a5c7d9b2 and 0794ddd13291 — partition_embedder_swaps."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _load(monkeypatch, name: str):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(f"services.persistence.migrations.alembic.versions.{name}")


@pytest.fixture
def migration(monkeypatch):
    return _load(monkeypatch, "e1f3a5c7d9b2_add_partition_embedder_swaps")


@pytest.fixture
def run_columns(monkeypatch):
    return _load(monkeypatch, "0794ddd13291_add_embedder_swap_run_columns")


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

    def add_column(self, table, column):
        self.calls.append(("add_column", table, column.name, column.server_default.arg))

    def alter_column(self, table, column, server_default):
        self.calls.append(("alter_column", table, column, server_default))

    def drop_column(self, table, column):
        self.calls.append(("drop_column", table, column))


def test_it_follows_the_per_partition_workspace_id_migration(migration):
    assert migration.down_revision == "a7b8c9d0e1f2"


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


def test_the_run_columns_follow_the_table(run_columns):
    assert run_columns.down_revision == "e1f3a5c7d9b2"


def test_a_table_from_an_earlier_push_gets_both_columns(monkeypatch, run_columns):
    op = _FakeOp()
    monkeypatch.setattr(run_columns, "op", op)
    monkeypatch.setattr(run_columns, "table_exists", lambda _t: True)
    monkeypatch.setattr(run_columns, "column_exists", lambda _t, _c: False)

    run_columns.upgrade()

    assert op.calls == [
        ("add_column", "partition_embedder_swaps", "chunks_over_window", "0"),
        ("add_column", "partition_embedder_swaps", "run_id", ""),
        ("alter_column", "partition_embedder_swaps", "run_id", None),
    ]


def test_the_run_columns_are_a_no_op_where_they_exist(monkeypatch, run_columns):
    op = _FakeOp()
    monkeypatch.setattr(run_columns, "op", op)
    monkeypatch.setattr(run_columns, "table_exists", lambda _t: True)
    monkeypatch.setattr(run_columns, "column_exists", lambda _t, _c: True)

    run_columns.upgrade()

    assert op.calls == []


def test_the_run_columns_downgrade_drops_only_what_exists(monkeypatch, run_columns):
    op = _FakeOp()
    monkeypatch.setattr(run_columns, "op", op)
    monkeypatch.setattr(run_columns, "table_exists", lambda _t: True)
    monkeypatch.setattr(run_columns, "column_exists", lambda _t, c: c == "run_id")

    run_columns.downgrade()

    assert op.calls == [("drop_column", "partition_embedder_swaps", "run_id")]
