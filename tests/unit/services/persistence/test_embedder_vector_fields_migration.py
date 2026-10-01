"""SQL revision c4e8f2a6b913: every embedder gets its own vector field."""

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
        "services.persistence.migrations.alembic.versions.c4e8f2a6b913_add_embedder_vector_fields",
    )


class _Result:
    def __init__(self, values: list) -> None:
        self._values = values

    def scalars(self):
        return iter(self._values)

    def scalar_one(self):
        return self._values[0]


class _FakeConn:
    """Answers the migration's queries and records its updates."""

    def __init__(self, *, taken=(), unallocated=(), defaults=(), on_alias: int = 0) -> None:
        self.taken, self.unallocated, self.defaults, self.on_alias = list(taken), list(unallocated), defaults, on_alias
        self.updates: list[tuple[str, dict]] = []

    def execute(self, statement, params: dict | None = None):
        sql = str(statement)
        if sql.startswith("SELECT COUNT(*)"):
            return _Result([self.on_alias])
        if sql.startswith("SELECT vector_field"):
            return _Result(self.taken)
        if sql.startswith("SELECT name") and "vector_field IS NULL" in sql:
            return _Result(self.unallocated)
        if sql.startswith("SELECT name") and "is_default" in sql:
            return _Result(list(self.defaults))
        self.updates.append((sql, params or {}))
        return _Result([])


class _FakeOp:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self.created: list[str] = []

    def get_bind(self):
        return self.conn

    def add_column(self, table, column) -> None:
        self.created.append(column.name)

    def create_index(self, name, *args, **kwargs) -> None:
        self.created.append(name)

    def create_check_constraint(self, name, *args, **kwargs) -> None:
        self.created.append(name)


def _upgrade(monkeypatch, migration, conn: _FakeConn, *, fresh_database: bool = False) -> _FakeOp:
    fake_op = _FakeOp(conn)
    monkeypatch.setattr(migration, "op", fake_op)
    # create_all() has already built every object on a fresh database.
    monkeypatch.setattr(migration, "column_exists", lambda table, _column: fresh_database or table == "partitions")
    monkeypatch.setattr(migration, "index_exists", lambda _table, _index: fresh_database)
    monkeypatch.setattr(migration, "check_constraint_exists", lambda _table, _name: fresh_database)
    migration.upgrade()
    return fake_op


def test_each_embedder_gets_a_distinct_field_oldest_first(monkeypatch, migration) -> None:
    conn = _FakeConn(taken=["vector_a_b"], unallocated=["a.b", "a-b", "Qwen3-Embedding-0.6B"])

    fake_op = _upgrade(monkeypatch, migration, conn)

    assert fake_op.created == ["vector_field", "uq_model_endpoint_vector_field", "ck_embedder_has_vector_field"]
    assert [(p["name"], p["field"]) for sql, p in conn.updates if "SET vector_field" in sql] == [
        ("a.b", "vector_a_b_2"),
        ("a-b", "vector_a_b_3"),
        ("Qwen3-Embedding-0.6B", "vector_Qwen3_Embedding_0_6B"),
    ]


def test_a_fresh_database_creates_nothing(monkeypatch, migration) -> None:
    assert _upgrade(monkeypatch, migration, _FakeConn(), fresh_database=True).created == []


def test_indexed_partitions_on_the_alias_are_pinned_to_the_default(monkeypatch, migration) -> None:
    conn = _FakeConn(defaults=["bge-m3"])

    _upgrade(monkeypatch, migration, conn)

    [(sql, params)] = [update for update in conn.updates if "SET embedder" in update[0]]
    assert params == {"name": "bge-m3", "alias": "default"}
    assert "EXISTS (SELECT 1 FROM files" in sql  # empty partitions keep following the default


@pytest.mark.parametrize("defaults", [[], ["a", "b"]])
def test_the_alias_cannot_be_pinned_without_exactly_one_default(monkeypatch, migration, defaults) -> None:
    with pytest.raises(RuntimeError, match="2 partition\\(s\\) holding files"):
        _upgrade(monkeypatch, migration, _FakeConn(defaults=defaults, on_alias=2))

    # Without indexed partitions on the alias there is nothing to pin.
    _upgrade(monkeypatch, migration, _FakeConn(defaults=defaults, on_alias=0))
