"""Unit tests for the v1 → v2 Milvus rebuild migration.

The migration is a numbered script loaded by path, so it is imported the same
way the runner imports it. Every Milvus call goes through a fake client that
records what the migration would do.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[4]
    / "openrag"
    / "services"
    / "persistence"
    / "migrations"
    / "milvus"
    / "2.rebuild_text_analyzer.py"
)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location("milvus_migration_v2", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

#: The analyzer OpenRAG shipped at schema version 1 — no `lowercase` filter.
_V1_ANALYZER = json.dumps({"tokenizer": "standard", "filter": [{"type": "stop", "stop_words": ["_english_"]}]})
_V2_ANALYZER = json.dumps({"tokenizer": "standard", "filter": ["lowercase", {"type": "stop", "stop_words": []}]})


def _desc(
    *,
    version: str | None = "1",
    analyzer: str = _V1_ANALYZER,
    enable_match: str | None = "true",
    hybrid: bool = True,
    dim: int = 8,
    text_max_length: int = 65_535,
) -> dict[str, Any]:
    """A describe_collection payload in the shape pymilvus returns one.

    Type params come back from the server as strings, which is what the
    migration's introspection helpers have to cope with.
    """
    text_params: dict[str, Any] = {
        "max_length": text_max_length,
        "enable_analyzer": "true",
        "analyzer_params": analyzer,
    }
    if enable_match is not None:
        text_params["enable_match"] = enable_match

    fields = [
        {"name": "_id", "params": {}},
        {"name": "text", "params": text_params},
        {"name": "partition", "params": {"max_length": 65535}},
        {"name": "file_id", "params": {"max_length": 65535}},
        {"name": "vector", "params": {"dim": dim}},
        {"name": "created_at", "params": {}},
    ]
    if hybrid:
        fields.append({"name": "sparse", "params": {}})

    properties = {} if version is None else {"openrag.schema_version": version}
    return {"fields": fields, "properties": properties}


class _SchemaRecorder:
    def __init__(self) -> None:
        self.fields: list[dict[str, Any]] = []
        self.functions: list[Any] = []

    def add_field(self, **kwargs) -> None:
        self.fields.append(kwargs)

    def add_function(self, function) -> None:
        self.functions.append(function)


class _IndexRecorder:
    def __init__(self) -> None:
        self.indexes: list[dict[str, Any]] = []

    def add_index(self, **kwargs) -> None:
        self.indexes.append(kwargs)


class _Iterator:
    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self._batches = list(batches)
        self.closed = False

    def next(self) -> list[dict[str, Any]]:
        return self._batches.pop(0) if self._batches else []

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """Records calls; returns descs keyed by collection name."""

    def __init__(
        self,
        source_desc: dict[str, Any],
        *,
        rows: list[dict[str, Any]] | None = None,
        existing: tuple[str, ...] = ("openrag",),
        target_count: int | None = None,
        source_count_after_copy: int | None = None,
    ) -> None:
        self._descs = {"openrag": source_desc}
        self._rows = rows if rows is not None else [{"_id": 1, "text": "Rapport", "vector": [0.1] * 8}]
        self._existing = set(existing)
        self._target_count = target_count
        self._source_count_after_copy = source_count_after_copy
        self._copied = False
        self.schema = _SchemaRecorder()
        self.index_params = _IndexRecorder()
        self.created: list[str] = []
        self.inserted: list[dict[str, Any]] = []
        self.renames: list[tuple[str, str]] = []
        self.properties: list[tuple[str, dict[str, str]]] = []
        self.dropped_properties: list[tuple[str, list[str]]] = []
        self.rename_failures: set[str] = set()
        self.released: list[str] = []
        self.calls: list[str] = []

    # -- introspection ------------------------------------------------
    def describe_collection(self, collection_name: str) -> dict[str, Any]:
        return self._descs[collection_name]

    def has_collection(self, collection_name: str) -> bool:
        return collection_name in self._existing

    def load_collection(self, collection_name: str) -> None:
        pass

    def release_collection(self, collection_name: str) -> None:
        self.released.append(collection_name)
        self.calls.append(f"release:{collection_name}")

    def query(self, collection_name: str, filter: str, output_fields: list[str]) -> list[dict[str, Any]]:  # noqa: A002
        if collection_name == "openrag":
            if self._copied and self._source_count_after_copy is not None:
                return [{"count(*)": self._source_count_after_copy}]
            return [{"count(*)": len(self._rows)}]
        count = self._target_count if self._target_count is not None else len(self.inserted)
        return [{"count(*)": count}]

    def query_iterator(self, **kwargs) -> _Iterator:
        self.batch_size = kwargs["batch_size"]
        self._copied = True
        return _Iterator([self._rows])

    # -- mutation -----------------------------------------------------
    def create_schema(self, **kwargs) -> _SchemaRecorder:
        return self.schema

    def prepare_index_params(self) -> _IndexRecorder:
        return self.index_params

    def create_collection(self, collection_name: str, **kwargs) -> None:
        self.created.append(collection_name)
        self._existing.add(collection_name)
        self._descs[collection_name] = _desc(analyzer=_V2_ANALYZER, enable_match=None)

    def insert(self, collection_name: str, data: list[dict[str, Any]]) -> None:
        self.inserted.extend(data)
        self.calls.append(f"insert:{collection_name}:{len(data)}")

    def flush(self, collection_name: str) -> None:
        pass

    def alter_collection_properties(self, collection_name: str, properties: dict[str, str]) -> None:
        self.properties.append((collection_name, properties))
        self.calls.append(f"alter:{collection_name}:{','.join(properties)}")

    def drop_collection_properties(self, collection_name: str, property_keys: list[str]) -> None:
        self.dropped_properties.append((collection_name, property_keys))
        self.calls.append(f"drop_props:{collection_name}:{','.join(property_keys)}")

    def rename_collection(self, old_name: str, new_name: str) -> None:
        self.calls.append(f"rename:{old_name}->{new_name}")
        if old_name in self.rename_failures:
            raise RuntimeError(f"rename of {old_name} refused")
        self.renames.append((old_name, new_name))
        self._existing.discard(old_name)
        self._existing.add(new_name)


# ---------------------------------------------------------------------------
# Skip paths — the collection is left alone, only the stamp moves
# ---------------------------------------------------------------------------


def test_already_at_target_version_does_nothing(migration) -> None:
    client = _FakeClient(_desc(version="2"))

    migration.upgrade(client, "openrag")

    assert client.created == []
    assert client.properties == []


def test_dense_only_collection_is_only_restamped(migration) -> None:
    # No `sparse` field means no BM25 leg, so the analyzer is never consulted.
    client = _FakeClient(_desc(hybrid=False))

    migration.upgrade(client, "openrag")

    assert client.created == []
    assert client.properties == [("openrag", {"openrag.schema_version": "2"})]


def test_collection_already_in_v2_shape_is_only_restamped(migration) -> None:
    client = _FakeClient(_desc(analyzer=_V2_ANALYZER, enable_match=None))

    migration.upgrade(client, "openrag")

    assert client.created == []
    assert client.properties == [("openrag", {"openrag.schema_version": "2"})]


def test_text_match_alone_still_forces_a_rebuild(migration) -> None:
    # The analyzer is already right, but `enable_match` cannot be turned off in
    # place — and while it is set, no later analyzer change is possible either.
    client = _FakeClient(_desc(analyzer=_V2_ANALYZER, enable_match="true"))

    migration.upgrade(client, "openrag")

    assert client.created == ["openrag_v2_rebuild"]


def test_dry_run_changes_nothing(migration) -> None:
    client = _FakeClient(_desc())

    migration.upgrade(client, "openrag", dry_run=True)

    assert client.created == []
    assert client.properties == []
    assert client.renames == []


# ---------------------------------------------------------------------------
# The rebuild
# ---------------------------------------------------------------------------


def test_rebuilt_text_field_drops_text_match_and_lowercases(migration) -> None:
    client = _FakeClient(_desc())

    migration.upgrade(client, "openrag")

    text = next(f for f in client.schema.fields if f["field_name"] == "text")
    assert "enable_match" not in text
    assert text["analyzer_params"]["filter"][0] == "lowercase"


def test_successful_rebuild_swaps_the_names(migration) -> None:
    client = _FakeClient(_desc())

    migration.upgrade(client, "openrag")

    assert client.renames == [("openrag", "openrag_v1_backup"), ("openrag_v2_rebuild", "openrag")]
    assert ("openrag_v2_rebuild", {"openrag.schema_version": "2"}) in client.properties


def test_an_interrupted_previous_run_refuses_to_start(migration) -> None:
    client = _FakeClient(_desc(), existing=("openrag", "openrag_v2_rebuild"))

    with pytest.raises(RuntimeError, match="already exists"):
        migration.upgrade(client, "openrag")


def test_row_count_mismatch_aborts_before_any_rename(migration) -> None:
    client = _FakeClient(_desc(), target_count=0)

    with pytest.raises(RuntimeError, match="Row-count mismatch"):
        migration.upgrade(client, "openrag")

    assert client.renames == []


def test_a_failed_swap_puts_the_original_collection_back(migration) -> None:
    # If the second rename fails the collection name resolves to nothing, so the
    # backup has to be renamed back rather than left aside.
    client = _FakeClient(_desc())
    client.rename_failures = {"openrag_v2_rebuild"}

    with pytest.raises(RuntimeError, match="rename of openrag_v2_rebuild refused"):
        migration.upgrade(client, "openrag")

    assert client.renames == [("openrag", "openrag_v1_backup"), ("openrag_v1_backup", "openrag")]
    assert client.has_collection("openrag")


# ---------------------------------------------------------------------------
# Copy paging
# ---------------------------------------------------------------------------


def test_copy_batch_size_shrinks_as_the_declared_text_length_grows(migration) -> None:
    small = migration._copy_batch_size(_desc(dim=1024, text_max_length=1_024))
    large = migration._copy_batch_size(_desc(dim=1024, text_max_length=65_535))
    assert small > large


def test_copy_batch_size_counts_every_declared_varchar(migration) -> None:
    # `partition` and `file_id` are declared as wide as `text`; a budget that
    # only charged for `text` would page three times too optimistically.
    narrow = _desc(dim=8)
    narrow["fields"] = [
        f if f["name"] not in ("partition", "file_id") else {**f, "params": {"max_length": 256}}
        for f in narrow["fields"]
    ]
    assert migration._copy_batch_size(narrow) > migration._copy_batch_size(_desc(dim=8))


def test_copy_batch_size_charges_for_the_sparse_column(migration) -> None:
    # `output_fields=["*"]` brings `sparse` back over the wire even though the
    # copy strips it before inserting, so the page has to have room for it.
    assert migration._copy_batch_size(_desc(hybrid=False)) > migration._copy_batch_size(_desc(hybrid=True))


def test_copy_batch_size_stays_positive_for_an_absurd_row(migration) -> None:
    assert migration._copy_batch_size(_desc(dim=4096, text_max_length=10 * 1024 * 1024)) >= 1


def test_copy_batch_size_is_capped(migration) -> None:
    tiny = _desc(dim=1, text_max_length=1, hybrid=False)
    tiny["fields"] = [
        f if f["name"] not in ("partition", "file_id") else {**f, "params": {"max_length": 1}} for f in tiny["fields"]
    ]
    assert migration._copy_batch_size(tiny) == migration.MAX_COPY_BATCH


def test_the_copy_pages_on_the_declared_schema(migration) -> None:
    client = _FakeClient(_desc())

    migration.upgrade(client, "openrag")

    assert client.batch_size == migration._copy_batch_size(_desc())


def test_the_copy_keeps_chunk_ids_and_drops_the_function_column(migration) -> None:
    # `sparse` is regenerated server-side and is rejected from a caller; `_id`
    # has to survive or every chunk id handed out earlier stops resolving.
    client = _FakeClient(
        _desc(),
        rows=[{"_id": 7, "sparse": {1: 0.5}, "text": "Rapport", "vector": [0.1] * 8, "file_id": "f"}],
    )

    migration.upgrade(client, "openrag")

    assert client.inserted == [{"_id": 7, "text": "Rapport", "vector": [0.1] * 8, "file_id": "f"}]


# ---------------------------------------------------------------------------
# auto_id override and concurrent-write detection
# ---------------------------------------------------------------------------


def test_auto_id_override_is_set_for_the_copy_and_dropped_before_the_swap(migration) -> None:
    # An `auto_id` primary key rejects caller-supplied values unless the
    # collection carries `allow_insert_auto_id`; it must not outlive the copy.
    client = _FakeClient(_desc())

    migration.upgrade(client, "openrag")

    assert ("openrag_v2_rebuild", {"allow_insert_auto_id": "true"}) in client.properties
    assert client.dropped_properties == [("openrag_v2_rebuild", ["allow_insert_auto_id"])]

    order = [
        c for c in client.calls if c.startswith(("alter:openrag_v2_rebuild:allow", "insert", "drop_props", "rename"))
    ]
    assert order[0].startswith("alter:openrag_v2_rebuild:allow")
    assert order[1].startswith("insert:")
    assert order[2].startswith("drop_props:")
    assert order[3].startswith("rename:")


def test_a_source_that_changes_during_the_copy_aborts_the_swap(migration) -> None:
    # OpenRAG is supposed to be stopped; if it was not, the copy is a stale
    # snapshot and swapping it in would lose the newer rows.
    client = _FakeClient(_desc(), source_count_after_copy=99)

    with pytest.raises(RuntimeError, match="changed during the copy"):
        migration.upgrade(client, "openrag")

    assert client.renames == []


# ---------------------------------------------------------------------------
# Memory: the collection moved aside must not stay resident
# ---------------------------------------------------------------------------


def test_the_backup_is_released_after_the_swap(migration) -> None:
    # Counting rows loads both collections and a rename does not unload them,
    # so without this the deployment carries two full copies in query-node
    # memory until Milvus restarts.
    client = _FakeClient(_desc())

    migration.upgrade(client, "openrag")

    assert client.released == ["openrag_v1_backup"]
    order = [c for c in client.calls if c.startswith(("rename:", "release:"))]
    assert order[-1] == "release:openrag_v1_backup"


def test_a_release_that_fails_does_not_fail_the_migration(migration) -> None:
    client = _FakeClient(_desc())
    client.release_collection = lambda name: (_ for _ in ()).throw(RuntimeError("release refused"))

    migration.upgrade(client, "openrag")

    assert client.renames == [("openrag", "openrag_v1_backup"), ("openrag_v2_rebuild", "openrag")]


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_rollback_swaps_the_backup_back_and_restamps(migration) -> None:
    client = _FakeClient(_desc(version="2"), existing=("openrag", "openrag_v1_backup"))

    migration.downgrade(client, "openrag")

    assert client.renames == [("openrag", "openrag_v2_rolled_back"), ("openrag_v1_backup", "openrag")]
    assert client.properties == [("openrag", {"openrag.schema_version": "1"})]
    assert client.released == ["openrag_v2_rolled_back"]


def test_a_failed_rollback_rename_puts_the_v2_collection_back(migration) -> None:
    # Same hazard as the upgrade swap: if the second rename fails the
    # configured collection name resolves to nothing and OpenRAG cannot start.
    client = _FakeClient(_desc(version="2"), existing=("openrag", "openrag_v1_backup"))
    client.rename_failures = {"openrag_v1_backup"}

    with pytest.raises(RuntimeError, match="rename of openrag_v1_backup refused"):
        migration.downgrade(client, "openrag")

    assert client.renames == [("openrag", "openrag_v2_rolled_back"), ("openrag_v2_rolled_back", "openrag")]
    assert client.has_collection("openrag")


def test_rollback_of_a_dense_only_collection_moves_the_stamp_back(migration) -> None:
    # The upgrade only re-stamped it — there was no analyzer to fix — so it
    # correctly has no backup, and the rollback is the stamp going back. The
    # old code returned here, leaving version 2 stamped on a v1 deployment.
    client = _FakeClient(_desc(version="2", hybrid=False), existing=("openrag",))

    migration.downgrade(client, "openrag")

    assert client.properties == [("openrag", {"openrag.schema_version": "1"})]
    assert client.renames == []


def test_dry_run_rollback_of_a_dense_only_collection_changes_nothing(migration) -> None:
    client = _FakeClient(_desc(version="2", hybrid=False), existing=("openrag",))

    migration.downgrade(client, "openrag", dry_run=True)

    assert client.properties == []


def test_rollback_of_a_rebuilt_collection_without_its_backup_fails_loudly(migration) -> None:
    # A hybrid collection at version 2 was rebuilt, so a missing backup means
    # the backup was lost — not that the upgrade skipped the rebuild.
    client = _FakeClient(_desc(version="2", analyzer=_V2_ANALYZER, enable_match=None), existing=("openrag",))

    with pytest.raises(RuntimeError, match="No backup collection"):
        migration.downgrade(client, "openrag")

    assert client.properties == []
