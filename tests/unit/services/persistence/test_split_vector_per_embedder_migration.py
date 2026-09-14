"""Unit tests for the v2 → v3 Milvus migration (one dense field per embedder).

Loaded by path, the way the runner loads it. Milvus is an in-memory fake that
holds real rows, so a test can run the whole upgrade and inspect the data it
leaves behind rather than only the calls it made.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pymilvus import DataType

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[4]
    / "openrag"
    / "services"
    / "persistence"
    / "migrations"
    / "milvus"
    / "3.split_vector_per_embedder.py"
)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("milvus_migration_v3", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _IndexParams:
    def __init__(self) -> None:
        self.indexes: list[dict[str, Any]] = []

    def add_index(self, **kwargs) -> None:
        self.indexes.append(kwargs)


class _Iterator:
    def __init__(self, rows: list[dict[str, Any]], batch_size: int) -> None:
        self._rows = rows
        self._batch = batch_size

    def next(self) -> list[dict[str, Any]]:
        page, self._rows = self._rows[: self._batch], self._rows[self._batch :]
        return page

    def close(self) -> None:
        pass


class FakeMilvus:
    def __init__(self, *, dim: int = 4, hybrid: bool = True, version: str = "2") -> None:
        self.fields: list[dict[str, Any]] = [
            {"name": "_id", "type": DataType.INT64, "params": {}},
            {"name": "partition", "type": DataType.VARCHAR, "params": {"max_length": 100}},
            {"name": "vector", "type": DataType.FLOAT_VECTOR, "params": {"dim": dim}},
        ]
        if hybrid:
            self.fields.append({"name": "sparse", "type": DataType.SPARSE_FLOAT_VECTOR, "params": {}})
        self.properties = {"openrag.schema_version": version}
        self.rows: list[dict[str, Any]] = []
        self.indexes: dict[str, dict[str, Any]] = {"vector": {}}
        self.calls: list[str] = []
        self.writes_during_copy = 0

    def add_rows(self, partition: str, n: int, field: str = "vector") -> None:
        for _ in range(n):
            _id = len(self.rows) + 1
            row = {f["name"]: None for f in self.fields if f["type"] == DataType.FLOAT_VECTOR}
            row.update({"_id": _id, "partition": partition, field: [float(_id)] * 4})
            self.rows.append(row)

    # -- filters ------------------------------------------------------------
    def _match(self, filter_expr: str) -> list[dict[str, Any]]:
        if not filter_expr:
            return list(self.rows)
        if m := re.fullmatch(r'partition == (".*")', filter_expr):
            name = json.loads(m.group(1))
            return [r for r in self.rows if r["partition"] == name]
        if m := re.fullmatch(r"partition in \[(.*)\]", filter_expr):
            names = set(json.loads(f"[{m.group(1)}]"))
            return [r for r in self.rows if r["partition"] in names]
        raise AssertionError(f"unexpected filter: {filter_expr}")

    # -- client surface -----------------------------------------------------
    def describe_collection(self, _name):
        return {"fields": [dict(f) for f in self.fields], "properties": dict(self.properties)}

    def load_collection(self, _name):
        self.calls.append("load")

    def refresh_load(self, _name):
        self.calls.append("refresh_load")

    def flush(self, _name):
        self.calls.append("flush")

    def query(self, collection_name, filter, output_fields):
        assert output_fields == ["count(*)"]
        return [{"count(*)": len(self._match(filter))}]

    def query_iterator(self, collection_name, filter, batch_size, output_fields):
        (field,) = output_fields
        return _Iterator([{"_id": r["_id"], field: r.get(field)} for r in self._match(filter)], batch_size)

    def upsert(self, collection_name, data, partial_update):
        assert partial_update is True
        by_id = {r["_id"]: r for r in self.rows}
        for item in data:
            by_id[item["_id"]].update({k: v for k, v in item.items() if k != "_id"})
        self.calls.append("upsert")
        for _ in range(self.writes_during_copy):
            self.add_rows("intruder", 1)
        self.writes_during_copy = 0

    def add_collection_field(self, collection_name, field_name, data_type, dim, nullable):
        assert nullable is True
        self.fields.append({"name": field_name, "type": data_type, "params": {"dim": dim}})
        for r in self.rows:
            r[field_name] = None
        self.calls.append(f"add_field:{field_name}")

    def prepare_index_params(self):
        return _IndexParams()

    def create_index(self, collection_name, index_params, sync=True):
        assert sync is False, "a synchronous build of a just-added field never finishes on existing rows"
        for i in index_params.indexes:
            self.indexes[i["field_name"]] = i
        self.calls.append("create_index:" + ",".join(i["field_name"] for i in index_params.indexes))

    def list_indexes(self, collection_name, field_name):
        return [field_name] if field_name in self.indexes else []

    def describe_index(self, collection_name, index_name):
        return {"indexed_rows": len(self.rows), "total_rows": len(self.rows), "pending_index_rows": 0}

    def drop_collection_field(self, collection_name, field_name):
        self.fields = [f for f in self.fields if f["name"] != field_name]
        self.indexes.pop(field_name, None)
        for r in self.rows:
            r.pop(field_name, None)
        self.calls.append(f"drop_field:{field_name}")

    def alter_collection_properties(self, collection_name, properties):
        self.properties.update(properties)

    # -- helpers ------------------------------------------------------------
    def field_names(self) -> list[str]:
        return [f["name"] for f in self.fields]


def _catalog(migration, partitions, embedders, default=None):
    return migration.Catalog(partitions=partitions, embedder_fields=embedders, default_embedder=default)


@pytest.fixture
def use_catalog(monkeypatch, migration):
    def install(catalog):
        monkeypatch.setattr(migration, "load_catalog", lambda _name: catalog)

    return install


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def test_rows_move_to_their_partitions_embedder_field_and_vector_is_dropped(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_rows("legacy", 3)
    client.add_rows("other", 2)
    before = {r["_id"]: r["vector"] for r in client.rows}
    use_catalog(
        _catalog(
            migration,
            {"legacy": "default", "other": "bge", "empty": "bge"},
            {"qwen": "vector_qwen", "bge": "vector_bge"},
            default="qwen",
        )
    )

    migration.upgrade(client, "c")

    assert "vector" not in client.field_names()
    assert {"vector_qwen", "vector_bge", "sparse"} <= set(client.field_names())
    for r in client.rows:
        target = "vector_qwen" if r["partition"] == "legacy" else "vector_bge"
        other = "vector_bge" if target == "vector_qwen" else "vector_qwen"
        assert r[target] == before[r["_id"]]
        assert r[other] is None
    assert client.properties["openrag.schema_version"] == "3"
    # The irreversible step comes last.
    assert client.calls.index("drop_field:vector") > max(i for i, c in enumerate(client.calls) if c == "upsert")


def test_an_existing_field_of_the_same_dimension_is_reused(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_collection_field("c", "vector_bge", DataType.FLOAT_VECTOR, 4, True)
    client.add_rows("p", 2)
    client.calls.clear()
    use_catalog(_catalog(migration, {"p": "bge"}, {"bge": "vector_bge"}))

    migration.upgrade(client, "c")

    assert not any(c.startswith("add_field") for c in client.calls)
    assert all(r["vector_bge"] is not None for r in client.rows)


def test_a_rerun_indexes_a_field_a_failed_run_added_but_never_indexed(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_collection_field("c", "vector_bge", DataType.FLOAT_VECTOR, 4, True)
    client.add_rows("p", 1)
    use_catalog(_catalog(migration, {"p": "bge"}, {"bge": "vector_bge"}))

    migration.upgrade(client, "c")

    assert "vector_bge" in client.indexes


def test_every_new_field_is_indexed_before_the_copy(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_rows("a", 1)
    client.add_rows("b", 1)
    use_catalog(_catalog(migration, {"a": "qa", "b": "qb"}, {"qa": "vector_a", "qb": "vector_b"}))

    migration.upgrade(client, "c")

    first_upsert = client.calls.index("upsert")
    assert client.calls.index("create_index:vector_a") < first_upsert
    assert client.calls.index("create_index:vector_b") < first_upsert


@pytest.mark.parametrize(
    ("partitions", "embedders", "default", "reason"),
    [
        ({"p": "bge"}, {"bge": None}, None, "has no vector field yet"),
        ({"p": "gone"}, {"bge": "vector_bge"}, None, "does not exist"),
        ({"p": "default"}, {"bge": "vector_bge"}, None, "no single default embedder"),
    ],
)
def test_unroutable_partitions_abort_before_any_change(
    migration, use_catalog, partitions, embedders, default, reason
) -> None:
    client = FakeMilvus()
    client.add_rows("p", 2)
    use_catalog(_catalog(migration, partitions, embedders, default))

    with pytest.raises(RuntimeError, match=reason):
        migration.upgrade(client, "c")

    assert client.field_names().count("vector") == 1
    assert "upsert" not in client.calls
    assert client.properties["openrag.schema_version"] == "2"


def test_a_partition_without_rows_needs_no_route(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_rows("p", 1)
    use_catalog(_catalog(migration, {"p": "bge", "empty": "gone"}, {"bge": "vector_bge"}))

    migration.upgrade(client, "c")

    assert client.properties["openrag.schema_version"] == "3"


def test_an_existing_field_with_another_dimension_aborts(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_collection_field("c", "vector_bge", DataType.FLOAT_VECTOR, 8, True)
    client.add_rows("p", 1)
    use_catalog(_catalog(migration, {"p": "bge"}, {"bge": "vector_bge"}))

    with pytest.raises(RuntimeError, match="dim 8"):
        migration.upgrade(client, "c")
    assert "vector" in client.field_names()


def test_the_vector_field_ceiling_is_checked_up_front(migration, use_catalog) -> None:
    client = FakeMilvus()
    for i in range(7):
        client.add_collection_field("c", f"vector_x{i}", DataType.FLOAT_VECTOR, 4, True)
    client.add_rows("p", 1)
    client.add_rows("q", 1)
    client.calls.clear()
    use_catalog(_catalog(migration, {"p": "a", "q": "b"}, {"a": "vector_a", "b": "vector_b"}))

    with pytest.raises(RuntimeError, match="limit of 10"):
        migration.upgrade(client, "c")
    assert client.calls == ["load"]  # planning only: nothing added, indexed or copied


def test_a_write_during_the_copy_stops_the_drop(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_rows("p", 2)
    client.writes_during_copy = 1
    use_catalog(_catalog(migration, {"p": "bge"}, {"bge": "vector_bge"}))

    with pytest.raises(RuntimeError, match="changed during the copy"):
        migration.upgrade(client, "c")

    assert "vector" in client.field_names()
    assert client.properties["openrag.schema_version"] == "2"


def test_a_short_copy_stops_the_drop(migration, use_catalog, monkeypatch) -> None:
    client = FakeMilvus()
    client.add_rows("p", 3)
    use_catalog(_catalog(migration, {"p": "bge"}, {"bge": "vector_bge"}))
    real_copy = migration._copy_partition

    def lossy_copy(*args):
        copied = real_copy(*args)
        client.rows[0]["vector_bge"] = None
        return copied

    monkeypatch.setattr(migration, "_copy_partition", lossy_copy)

    with pytest.raises(RuntimeError, match="holds 2 vector"):
        migration.upgrade(client, "c")
    assert "vector" in client.field_names()


def test_rerunning_after_a_failed_verify_completes(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_rows("p", 2)
    client.writes_during_copy = 1
    use_catalog(_catalog(migration, {"p": "bge", "intruder": "bge"}, {"bge": "vector_bge"}))
    with pytest.raises(RuntimeError):
        migration.upgrade(client, "c")

    migration.upgrade(client, "c")

    assert "vector" not in client.field_names()
    assert all(r["vector_bge"] is not None for r in client.rows)


def test_unrouted_rows_do_not_block_the_upgrade(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_rows("p", 1)
    client.add_rows("orphan", 2)
    use_catalog(_catalog(migration, {"p": "bge"}, {"bge": "vector_bge"}))

    migration.upgrade(client, "c")

    assert client.properties["openrag.schema_version"] == "3"
    assert [r["vector_bge"] is not None for r in client.rows] == [True, False, False]


def test_dry_run_changes_nothing(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_rows("p", 2)
    use_catalog(_catalog(migration, {"p": "bge"}, {"bge": "vector_bge"}))

    migration.upgrade(client, "c", dry_run=True)

    assert client.field_names() == ["_id", "partition", "vector", "sparse"]
    assert client.properties["openrag.schema_version"] == "2"


def test_already_migrated_is_a_no_op(migration, use_catalog) -> None:
    client = FakeMilvus(version="3")
    use_catalog(None)

    migration.upgrade(client, "c")

    assert client.calls == []


def test_a_collection_without_vector_is_only_stamped(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.drop_collection_field("c", "vector")
    client.calls.clear()
    use_catalog(None)

    migration.upgrade(client, "c")

    assert client.properties["openrag.schema_version"] == "3"
    assert client.calls == []


def test_without_sparse_or_rows_the_last_vector_field_is_kept(migration, use_catalog) -> None:
    client = FakeMilvus(hybrid=False)
    use_catalog(_catalog(migration, {"p": "bge"}, {"bge": "vector_bge"}))

    with pytest.raises(RuntimeError, match="cannot be dropped"):
        migration.upgrade(client, "c")
    assert "vector" in client.field_names()


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def test_downgrade_restores_vector_from_each_partitions_field(migration, use_catalog) -> None:
    client = FakeMilvus()
    client.add_rows("a", 2)
    client.add_rows("b", 1)
    before = {r["_id"]: r["vector"] for r in client.rows}
    catalog = _catalog(migration, {"a": "qwen", "b": "bge"}, {"qwen": "vector_qwen", "bge": "vector_bge"})
    use_catalog(catalog)
    migration.upgrade(client, "c")

    migration.downgrade(client, "c")

    assert client.field_names() == ["_id", "partition", "sparse", "vector"]
    assert {r["_id"]: r["vector"] for r in client.rows} == before
    assert client.properties["openrag.schema_version"] == "2"


def test_downgrade_refuses_fields_of_different_dimensions(migration, use_catalog) -> None:
    client = FakeMilvus(version="3")
    client.drop_collection_field("c", "vector")
    client.add_collection_field("c", "vector_a", DataType.FLOAT_VECTOR, 4, True)
    client.add_collection_field("c", "vector_b", DataType.FLOAT_VECTOR, 8, True)
    client.add_rows("pa", 1, field="vector_a")
    client.add_rows("pb", 1, field="vector_b")
    use_catalog(_catalog(migration, {"pa": "a", "pb": "b"}, {"a": "vector_a", "b": "vector_b"}))

    with pytest.raises(RuntimeError, match="different dimensions"):
        migration.downgrade(client, "c")
    assert "vector" not in client.field_names()


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_an_unreachable_postgres_names_the_fix(migration, monkeypatch) -> None:
    # `docker compose run --no-deps` leaves `rdb` stopped, which surfaced as a
    # bare socket.gaierror from deep inside asyncpg.
    import socket

    import asyncpg

    async def refuse(**_kwargs):
        raise socket.gaierror(-3, "Temporary failure in name resolution")

    monkeypatch.setattr(asyncpg, "connect", refuse)

    with pytest.raises(RuntimeError, match="docker compose up -d rdb"):
        migration.load_catalog("vdb_test")
