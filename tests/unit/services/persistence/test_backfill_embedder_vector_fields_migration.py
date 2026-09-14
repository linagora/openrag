"""Tests for the migration that gives every legacy embedder a vector field."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from core.vector_stores.vector_field import allocate_vector_field_name


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.c4e8f2a6b913_backfill_embedder_vector_fields",
    )


class _Result:
    def __init__(self, values: list) -> None:
        self._values = values

    def scalars(self):
        return iter(self._values)


class _FakeConn:
    """Just enough of a Connection to answer the migration's four queries."""

    def __init__(self, *, taken: list[str], legacy: list[str], defaults: list[str]) -> None:
        self.taken = taken
        self.legacy = legacy
        self.defaults = defaults
        self.updates: list[tuple[str, dict]] = []

    def execute(self, statement, params: dict | None = None):
        sql = str(statement)
        if sql.startswith("SELECT vector_field"):
            return _Result(self.taken)
        if sql.startswith("SELECT name") and "vector_field IS NULL" in sql:
            return _Result(self.legacy)
        if sql.startswith("SELECT name") and "is_default" in sql:
            return _Result(self.defaults)
        self.updates.append((sql, params or {}))
        return _Result([])


class _FakeOp:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_bind(self):
        return self.conn

    def create_check_constraint(self, *args, **kwargs) -> None:
        self.calls.append(("create_check_constraint", args, kwargs))

    def drop_constraint(self, *args, **kwargs) -> None:
        self.calls.append(("drop_constraint", args, kwargs))


def _install(monkeypatch, migration, conn: _FakeConn, *, constraint_exists: bool = False) -> _FakeOp:
    fake_op = _FakeOp(conn)
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "column_exists", lambda _table, _column: True)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "check_constraint_exists", lambda _table, _name: constraint_exists)
    return fake_op


def _field_updates(conn: _FakeConn) -> list[tuple[str, str]]:
    return [(p["name"], p["field"]) for sql, p in conn.updates if "SET vector_field" in sql]


def _partition_updates(conn: _FakeConn) -> list[dict]:
    return [p for sql, p in conn.updates if "SET embedder" in sql]


@pytest.mark.parametrize(
    ("name", "taken"),
    [
        ("Qwen3-Embedding-0.6B", set()),
        ("a.b", {"vector_a_b"}),
        ("a-b", {"vector_a_b", "vector_a_b_2"}),
        ("...", set()),
        ("x" * 300, {"vector_" + "x" * 248}),
    ],
)
def test_the_frozen_allocator_matches_the_application_today(migration, name, taken) -> None:
    assert migration._allocate(name, taken) == allocate_vector_field_name(name, taken)


def test_each_legacy_embedder_gets_a_distinct_field_in_order(monkeypatch, migration) -> None:
    conn = _FakeConn(taken=["vector_a_b"], legacy=["a.b", "a-b", "Qwen3-Embedding-0.6B"], defaults=[])
    _install(monkeypatch, migration, conn)

    migration.upgrade()

    assert _field_updates(conn) == [
        ("a.b", "vector_a_b_2"),
        ("a-b", "vector_a_b_3"),
        ("Qwen3-Embedding-0.6B", "vector_Qwen3_Embedding_0_6B"),
    ]


def test_partitions_on_the_alias_are_pinned_to_the_single_default(monkeypatch, migration) -> None:
    conn = _FakeConn(taken=[], legacy=[], defaults=["Qwen3-Embedding-0.6B"])
    _install(monkeypatch, migration, conn)

    migration.upgrade()

    assert _partition_updates(conn) == [{"name": "Qwen3-Embedding-0.6B", "alias": "default"}]


@pytest.mark.parametrize("defaults", [[], ["a", "b"]])
def test_partitions_are_left_alone_without_exactly_one_default(monkeypatch, migration, defaults) -> None:
    conn = _FakeConn(taken=[], legacy=[], defaults=defaults)
    _install(monkeypatch, migration, conn)

    migration.upgrade()

    assert _partition_updates(conn) == []


def test_upgrade_adds_the_constraint_once(monkeypatch, migration) -> None:
    conn = _FakeConn(taken=[], legacy=[], defaults=[])
    fake_op = _install(monkeypatch, migration, conn)
    migration.upgrade()
    assert fake_op.calls == [
        (
            "create_check_constraint",
            ("ck_embedder_has_vector_field", "model_endpoints", migration._CONSTRAINT_SQL),
            {},
        )
    ]

    fake_op = _install(monkeypatch, migration, conn, constraint_exists=True)
    migration.upgrade()
    assert fake_op.calls == []


def test_upgrade_is_a_no_op_without_the_column(monkeypatch, migration) -> None:
    conn = _FakeConn(taken=[], legacy=["a"], defaults=["a"])
    fake_op = _install(monkeypatch, migration, conn)
    monkeypatch.setattr(migration, "column_exists", lambda _table, _column: False)

    migration.upgrade()

    assert conn.updates == []
    assert fake_op.calls == []


def test_downgrade_drops_only_the_constraint(monkeypatch, migration) -> None:
    conn = _FakeConn(taken=[], legacy=[], defaults=[])
    fake_op = _install(monkeypatch, migration, conn, constraint_exists=True)

    migration.downgrade()

    assert fake_op.calls == [
        ("drop_constraint", ("ck_embedder_has_vector_field", "model_endpoints"), {"type_": "check"})
    ]
    assert conn.updates == []
