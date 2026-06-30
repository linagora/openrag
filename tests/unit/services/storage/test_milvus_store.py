"""Unit tests for the pure-logic surface of :class:`MilvusVectorStore`.

These tests instantiate the store with both Milvus clients mocked out, so they
exercise filter-expression construction, ID coercion, entity layering, and the
``collection`` argument discipline without touching a live Milvus.

Integration tests that round-trip through a real Milvus 2.6 container live in
:mod:`test_milvus_store_integration` and are gated by the ``integration``
pytest marker.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from openrag.core.config.infrastructure import VectorDBConfig
from openrag.core.models.chunk import Chunk, ChunkType
from openrag.core.utils.exceptions import VDBSearchError
from openrag.services.storage.milvus_store import MilvusVectorStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vdb_config() -> VectorDBConfig:
    """A config bound to a non-default collection name so ``_resolve_collection``
    has a real value to validate against (a ``vdb_test`` default would collide
    with the ABC sentinel in some assertions).
    """
    return VectorDBConfig(
        host="milvus-test",
        port=19530,
        collection_name="test_collection",
        hybrid_search=True,
        schema_version=1,
    )


@pytest.fixture
def store(vdb_config: VectorDBConfig, monkeypatch: pytest.MonkeyPatch) -> MilvusVectorStore:
    """A ``MilvusVectorStore`` with both pymilvus clients mocked.

    Use this for pure-logic tests. Methods that drive the client (``upsert``,
    ``search``, ...) will hit the mocks; assert on mock calls if you must.

    We monkeypatch the symbols directly inside the loaded module rather than
    using ``unittest.mock.patch`` because the project's pythonpath setup
    (``pythonpath = ./openrag`` plus the ``openrag`` package itself on
    sys.path) lets the same source file get registered under two different
    module names depending on the import form, which makes string-based
    patch paths fragile.
    """
    import openrag.services.storage.milvus_store as _store_mod

    monkeypatch.setattr(_store_mod, "MilvusClient", MagicMock())
    monkeypatch.setattr(_store_mod, "AsyncMilvusClient", MagicMock())
    return MilvusVectorStore(vdb_config)


# ---------------------------------------------------------------------------
# _format_value
# ---------------------------------------------------------------------------


class TestFormatValue:
    def test_int_renders_unquoted(self) -> None:
        assert MilvusVectorStore._format_value(42) == "42"

    def test_float_renders_unquoted(self) -> None:
        assert MilvusVectorStore._format_value(3.14) == "3.14"

    def test_true_renders_lowercase(self) -> None:
        assert MilvusVectorStore._format_value(True) == "true"

    def test_false_renders_lowercase(self) -> None:
        # bool is an int subclass — make sure we hit the bool branch first.
        assert MilvusVectorStore._format_value(False) == "false"

    def test_string_is_double_quoted(self) -> None:
        assert MilvusVectorStore._format_value("alice") == '"alice"'

    def test_string_escapes_double_quotes(self) -> None:
        assert MilvusVectorStore._format_value('a"b') == '"a\\"b"'

    def test_string_escapes_backslashes(self) -> None:
        assert MilvusVectorStore._format_value("a\\b") == '"a\\\\b"'

    def test_string_escape_order(self) -> None:
        # Backslash must be escaped before the quote so we don't double-escape
        # the quote's preceding backslash.
        assert MilvusVectorStore._format_value('a\\"b') == '"a\\\\\\"b"'


# ---------------------------------------------------------------------------
# _build_filter_expr
# ---------------------------------------------------------------------------


class TestBuildFilterExpr:
    def test_none_yields_empty(self, store: MilvusVectorStore) -> None:
        assert store._build_filter_expr(None) == ""

    def test_empty_dict_yields_empty(self, store: MilvusVectorStore) -> None:
        assert store._build_filter_expr({}) == ""

    def test_scalar_partition(self, store: MilvusVectorStore) -> None:
        assert store._build_filter_expr({"partition": "p1"}) == 'partition == "p1"'

    def test_list_partition(self, store: MilvusVectorStore) -> None:
        expr = store._build_filter_expr({"partition": ["p1", "p2"]})
        assert expr == 'partition in ["p1", "p2"]'

    def test_partition_wildcard_is_skipped(self, store: MilvusVectorStore) -> None:
        # 'all' is the documented wildcard — should produce no partition clause.
        assert store._build_filter_expr({"partition": "all"}) == ""

    def test_partition_wildcard_alone_in_list_is_skipped(self, store: MilvusVectorStore) -> None:
        # Wildcard on its own in a list is still a wildcard — no partition clause.
        assert store._build_filter_expr({"partition": ["all"]}) == ""

    def test_partition_wildcard_mixed_with_explicit_raises(self, store: MilvusVectorStore) -> None:
        # Mixing the wildcard with explicit partitions would silently widen the
        # query/delete scope to every partition. Reject rather than absorb.
        with pytest.raises(ValueError, match="cannot mix wildcard"):
            store._build_filter_expr({"partition": ["all", "p1"]})

    def test_empty_partition_list_is_skipped(self, store: MilvusVectorStore) -> None:
        # No partitions means no partition clause (not match-nothing).
        assert store._build_filter_expr({"partition": []}) == ""

    def test_scalar_field(self, store: MilvusVectorStore) -> None:
        assert store._build_filter_expr({"file_id": "abc"}) == 'file_id == "abc"'

    def test_list_field_becomes_in(self, store: MilvusVectorStore) -> None:
        expr = store._build_filter_expr({"file_id": ["a", "b"]})
        assert expr == 'file_id in ["a", "b"]'

    def test_empty_list_field_matches_nothing(self, store: MilvusVectorStore) -> None:
        # An empty IN list cannot be expressed in Milvus, so short-circuit to
        # the explicit no-match literal — callers get an empty result set
        # instead of a syntax error.
        assert store._build_filter_expr({"file_id": []}) == "false"

    def test_raw_expr_appended(self, store: MilvusVectorStore) -> None:
        expr = store._build_filter_expr({"expr": "created_at > ISO '2025-01-01'"})
        assert expr == "created_at > ISO '2025-01-01'"

    def test_raw_expr_combined_with_partition(self, store: MilvusVectorStore) -> None:
        # Multiple predicates are each parenthesised so the raw user expr cannot
        # escape the partition scope via operator precedence.
        expr = store._build_filter_expr({"partition": "p1", "expr": "page > 5"})
        assert expr == '(partition == "p1") and (page > 5)'

    def test_partition_and_field_joined_with_and(self, store: MilvusVectorStore) -> None:
        expr = store._build_filter_expr({"partition": "p1", "file_id": "f1"})
        assert expr == '(partition == "p1") and (file_id == "f1")'

    def test_int_value_passes_through(self, store: MilvusVectorStore) -> None:
        assert store._build_filter_expr({"page": 7}) == "page == 7"

    def test_user_expr_with_or_cannot_escape_partition_scope(self, store: MilvusVectorStore) -> None:
        # A viewer-supplied filter with `or` must stay contained within the
        # partition predicate: `and` binds tighter than `or` in Milvus, so
        # without parentheses this would read another tenant's chunks.
        expr = store._build_filter_expr({"partition": "p1", "expr": 'text != "" or partition == "other"'})
        assert expr == '(partition == "p1") and (text != "" or partition == "other")'


# ---------------------------------------------------------------------------
# _resolve_collection
# ---------------------------------------------------------------------------


class TestResolveCollection:
    def test_bound_name_passes(self, store: MilvusVectorStore) -> None:
        assert store._resolve_collection("test_collection") == "test_collection"

    def test_default_sentinel_passes(self, store: MilvusVectorStore) -> None:
        # The ABC default 'default' resolves to the bound collection — without
        # this, every ABC-typed caller that omits the kwarg would crash.
        assert store._resolve_collection("default") == "test_collection"

    def test_other_name_raises(self, store: MilvusVectorStore) -> None:
        with pytest.raises(ValueError, match=re.escape("test_collection")):
            store._resolve_collection("some_other_collection")

    def test_error_mentions_partition_guidance(self, store: MilvusVectorStore) -> None:
        # The error must tell callers where partitions actually go, otherwise
        # the failure looks like a generic bad-arg and people retry with the
        # partition name as the collection.
        with pytest.raises(ValueError, match="partitions go in filters"):
            store._resolve_collection("bad-name")


# ---------------------------------------------------------------------------
# ID round-trip
# ---------------------------------------------------------------------------


class TestIdRoundTrip:
    def test_numeric_string_coerces(self) -> None:
        assert MilvusVectorStore._str_id_to_milvus("12345") == 12345

    def test_non_numeric_returns_none(self) -> None:
        # UUIDs (e.g. freshly-built Chunks before insert) must not crash a batch
        # delete — the contract is "silently skip", asserted here.
        assert MilvusVectorStore._str_id_to_milvus("not-a-number") is None

    def test_empty_string_returns_none(self) -> None:
        assert MilvusVectorStore._str_id_to_milvus("") is None

    def test_int_to_string_roundtrip(self) -> None:
        assert MilvusVectorStore._milvus_id_to_str(12345) == "12345"

    def test_full_roundtrip(self) -> None:
        original = 9876543210
        as_str = MilvusVectorStore._milvus_id_to_str(original)
        back = MilvusVectorStore._str_id_to_milvus(as_str)
        assert back == original


# ---------------------------------------------------------------------------
# _gen_chunk_order_metadata
# ---------------------------------------------------------------------------


class TestChunkOrderMetadata:
    def test_zero_chunks(self) -> None:
        assert MilvusVectorStore._gen_chunk_order_metadata(0) == []

    def test_single_chunk_has_no_neighbours(self) -> None:
        out = MilvusVectorStore._gen_chunk_order_metadata(1)
        assert len(out) == 1
        assert out[0]["prev_section_id"] is None
        assert out[0]["next_section_id"] is None
        assert isinstance(out[0]["section_id"], int)

    def test_three_chunks_form_linked_list(self) -> None:
        out = MilvusVectorStore._gen_chunk_order_metadata(3)
        # Mid chunk points to both neighbours.
        assert out[1]["prev_section_id"] == out[0]["section_id"]
        assert out[1]["next_section_id"] == out[2]["section_id"]
        # Edges have one None each.
        assert out[0]["prev_section_id"] is None
        assert out[2]["next_section_id"] is None
        # Section IDs are monotonically increasing within a batch.
        assert out[0]["section_id"] < out[1]["section_id"] < out[2]["section_id"]

    def test_two_concurrent_calls_have_disjoint_ranges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import services.storage.milvus_store as store_mod

        monkeypatch.setattr(store_mod.time, "time_ns", lambda: 123456789)

        a = MilvusVectorStore._gen_chunk_order_metadata(200)
        b = MilvusVectorStore._gen_chunk_order_metadata(200)
        ids_a = {row["section_id"] for row in a}
        ids_b = {row["section_id"] for row in b}
        assert ids_a.isdisjoint(ids_b)

    def test_ids_fit_in_int64(self) -> None:
        rows = MilvusVectorStore._gen_chunk_order_metadata(10_000)
        int64_max = 2**63 - 1
        for row in rows:
            assert 0 <= row["section_id"] < int64_max


# ---------------------------------------------------------------------------
# _safe_batch_size — shrink the query_iterator page when vectors are requested
# ---------------------------------------------------------------------------


class TestSafeBatchSize:
    def test_explicit_scalar_projection_keeps_large_default(self, store: MilvusVectorStore) -> None:
        # No vector and no wildcard → tiny rows → keep the large default page.
        assert store._safe_batch_size(["_id"]) == 16_000
        assert store._safe_batch_size(["partition", "file_id"]) == 16_000

    def test_wildcard_is_treated_as_vector_inclusive(self, store: MilvusVectorStore) -> None:
        # Milvus 2.6 returns the dense vector for ``["*"]`` too, so a wildcard
        # page must shrink even without an explicit ``"vector"`` field.
        store._embedding_dimension = 1024
        assert store._safe_batch_size(["*"]) == 3_276

    def test_vector_request_shrinks_page(self, store: MilvusVectorStore) -> None:
        store._embedding_dimension = 1024
        # 32 MiB budget / (1024*4 + 6144) bytes-per-row = 3276, well under 16k.
        assert store._safe_batch_size(["*", "vector"]) == 3_276

    def test_larger_embedder_shrinks_further(self, store: MilvusVectorStore) -> None:
        store._embedding_dimension = 4096
        page = store._safe_batch_size(["*", "vector"])
        assert page == (32 * 1024 * 1024) // (4096 * 4 + 6_144)  # 1489
        assert page < 3_276  # bigger vectors → fewer rows per page

    def test_unknown_dimension_reads_collection_schema(self, store: MilvusVectorStore) -> None:
        # Read-only process: dim unset (never indexed), but the live schema
        # knows the real dimension — page must size to *that*, not a guess.
        assert store._embedding_dimension is None
        store._client.describe_collection.return_value = {
            "fields": [
                {"name": "text", "params": {}},
                {"name": "vector", "params": {"dim": 4096}},
            ]
        }
        page = store._safe_batch_size(["*"])
        assert page == (32 * 1024 * 1024) // (4096 * 4 + 6_144)  # 1489
        # Resolved dim is cached so we don't re-probe Milvus on every query.
        assert store._embedding_dimension == 4096

    def test_unknown_dimension_falls_back_to_conservative_cap(self, store: MilvusVectorStore) -> None:
        import services.storage.milvus_store as store_mod

        # Schema probe fails (e.g. collection absent) → conservative large dim,
        # never a small guess that would under-size a high-dim collection's page.
        store._client.describe_collection.side_effect = RuntimeError("no collection")
        page = store._safe_batch_size(["vector"])
        assert page == (32 * 1024 * 1024) // (store_mod._UNKNOWN_VECTOR_DIM * 4 + 6_144)
        assert store._embedding_dimension is None  # not cached on failure

    def test_page_never_exceeds_default_cap(self, store: MilvusVectorStore) -> None:
        store._embedding_dimension = 1
        assert store._safe_batch_size(["vector"]) <= 16_000


# ---------------------------------------------------------------------------
# _chunk_to_entity
# ---------------------------------------------------------------------------


def _make_chunk(**overrides: Any) -> Chunk:
    defaults: dict[str, Any] = {
        "text": "hello",
        "document_id": "doc-1",
        "partition": "p1",
        "embedding": [0.1, 0.2, 0.3],
        "chunk_type": ChunkType.TEXT,
        "metadata": {"author": "alice"},
    }
    defaults.update(overrides)
    return Chunk(**defaults)


class TestChunkToEntity:
    @staticmethod
    def _entity(**overrides: Any) -> dict[str, Any]:
        chunk = _make_chunk(**overrides)
        order = {"prev_section_id": 1, "section_id": 2, "next_section_id": 3}
        return MilvusVectorStore._chunk_to_entity(
            chunk,
            indexed_at="2026-01-01T00:00:00+00:00",
            order=order,
        )

    def test_typed_fields_present(self) -> None:
        entity = self._entity()
        assert entity["text"] == "hello"
        assert entity["partition"] == "p1"
        assert entity["file_id"] == "doc-1"
        assert entity["vector"] == [0.1, 0.2, 0.3]
        assert entity["chunk_type"] == "text"

    def test_indexed_at_stamped(self) -> None:
        assert self._entity()["indexed_at"] == "2026-01-01T00:00:00+00:00"

    def test_order_metadata_merged(self) -> None:
        entity = self._entity()
        assert entity["prev_section_id"] == 1
        assert entity["section_id"] == 2
        assert entity["next_section_id"] == 3

    def test_metadata_passthrough(self) -> None:
        # Arbitrary metadata keys flow into the entity by design (dynamic schema).
        assert self._entity()["author"] == "alice"

    def test_typed_fields_win_over_metadata(self) -> None:
        # If caller-supplied metadata collides with a typed field, the typed
        # value wins — strict domain model > free-form dict.
        entity = self._entity(metadata={"partition": "WRONG", "file_id": "WRONG"})
        assert entity["partition"] == "p1"
        assert entity["file_id"] == "doc-1"

    def test_none_optional_fields_are_omitted(self) -> None:
        # token_count/header/context/content default to None; they must not
        # be stamped into the dynamic schema as nulls.
        entity = self._entity()
        for absent in ("token_count", "header", "context", "content"):
            assert absent not in entity, f"{absent} should be omitted when None"

    def test_set_optional_fields_present(self) -> None:
        entity = self._entity(token_count=42, header="H1", context="ctx", content="C")
        assert entity["token_count"] == 42
        assert entity["header"] == "H1"
        assert entity["context"] == "ctx"
        assert entity["content"] == "C"

    def test_id_is_not_in_entity(self) -> None:
        # Milvus assigns _id via auto_id=True; including it in the payload
        # would be rejected on insert.
        entity = self._entity()
        assert "_id" not in entity


# ---------------------------------------------------------------------------
# Surface-level ABC-vs-bound-collection enforcement
# ---------------------------------------------------------------------------


class TestCollectionArgDiscipline:
    @pytest.mark.asyncio
    async def test_upsert_rejects_foreign_collection(self, store: MilvusVectorStore) -> None:
        with pytest.raises(ValueError, match="test_collection"):
            await store.upsert([_make_chunk()], collection="some-other-name")

    @pytest.mark.asyncio
    async def test_search_rejects_foreign_collection(self, store: MilvusVectorStore) -> None:
        with pytest.raises(ValueError):
            await store.search([0.1, 0.2], collection="some-other-name")

    @pytest.mark.asyncio
    async def test_delete_rejects_foreign_collection(self, store: MilvusVectorStore) -> None:
        with pytest.raises(ValueError):
            await store.delete(["1"], collection="some-other-name")

    @pytest.mark.asyncio
    async def test_drop_rejects_foreign_collection(self, store: MilvusVectorStore) -> None:
        with pytest.raises(ValueError):
            await store.drop_collection("some-other-name")

    @pytest.mark.asyncio
    async def test_collection_exists_returns_false_for_foreign(self, store: MilvusVectorStore) -> None:
        # Falsifies rather than raising — `collection_exists` is asked
        # questions about names it does not own and answers "no, not here".
        assert await store.collection_exists("some-other-name") is False

    @pytest.mark.asyncio
    async def test_upsert_empty_list_is_noop(self, store: MilvusVectorStore) -> None:
        # No client calls should happen, and the return must be 0.
        result = await store.upsert([])
        assert result == 0
        store._async_client.insert.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_delete_empty_list_is_noop(self, store: MilvusVectorStore) -> None:
        result = await store.delete([])
        assert result == 0
        store._async_client.delete.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_delete_by_filter_empty_filter_raises(self, store: MilvusVectorStore) -> None:
        # Guards against an accidental wildcard wiping the whole collection.
        with pytest.raises(ValueError, match="drop_collection"):
            await store.delete_by_filter({})

    @pytest.mark.asyncio
    async def test_delete_by_filter_partition_wildcard_raises(self, store: MilvusVectorStore) -> None:
        # 'all' produces an empty expression — same guard must fire.
        with pytest.raises(ValueError, match="drop_collection"):
            await store.delete_by_filter({"partition": "all"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tautology", ["1==1", "1 == 1", "true", "TRUE", " True "])
    async def test_delete_by_filter_tautological_expr_raises(self, store: MilvusVectorStore, tautology: str) -> None:
        # Raw `expr` tautologies bypass the dict-form guards but would still
        # delete every row — the safety contract must reject them too.
        with pytest.raises(ValueError, match="drop_collection"):
            await store.delete_by_filter({"expr": tautology})


# ---------------------------------------------------------------------------
# Hybrid dispatch
# ---------------------------------------------------------------------------


class TestHybridDispatch:
    @pytest.mark.asyncio
    async def test_hybrid_disabled_store_routes_to_dense(
        self,
        vdb_config: VectorDBConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``search()`` on a store built with ``hybrid_search=False`` must take
        the dense path — the collection has no ``sparse`` field, so the BM25
        leg must never be reached.
        """
        import openrag.services.storage.milvus_store as _store_mod

        monkeypatch.setattr(_store_mod, "MilvusClient", MagicMock())
        monkeypatch.setattr(_store_mod, "AsyncMilvusClient", MagicMock())
        cfg = vdb_config.model_copy(update={"hybrid_search": False})
        store = MilvusVectorStore(cfg)
        store._async_client.search = AsyncMock(return_value=[])
        store._async_client.hybrid_search = AsyncMock(return_value=[])

        result = await store.search([0.1, 0.2], collection="default")

        assert result == []
        store._async_client.search.assert_awaited_once()
        store._async_client.hybrid_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_hybrid_store_requires_query_text(self, store: MilvusVectorStore) -> None:
        """The ``store`` fixture is hybrid-enabled; its BM25 leg has no input
        when ``query_text`` is omitted, so ``search()`` must refuse rather
        than silently drop the lexical signal.
        """
        with pytest.raises(VDBSearchError, match="query_text"):
            await store.search([0.1, 0.2], collection="default")


# ---------------------------------------------------------------------------
# _parse_search_response
# ---------------------------------------------------------------------------


class TestParseSearchResponse:
    """Milvus 2.6 exposes the ``_id`` auto-id PK on the hit (and in the entity),
    never under the generic ``id`` key — the parser must surface the real id."""

    def test_id_taken_from_hit_underscore_id(self, store: MilvusVectorStore) -> None:
        hit = {
            "_id": 466609479666371445,
            "distance": 0.42,
            "entity": {"_id": 466609479666371445, "text": "hello", "file_id": "doc1", "vector": [0.1, 0.2]},
        }
        (record,) = store._parse_search_response([[hit]])
        assert record["id"] == "466609479666371445"  # not the literal "None"
        assert record["score"] == 0.42
        assert "vector" not in record

    def test_id_falls_back_to_entity_underscore_id(self, store: MilvusVectorStore) -> None:
        hit = {"distance": 0.1, "entity": {"_id": 123, "text": "t"}}
        (record,) = store._parse_search_response([[hit]])
        assert record["id"] == "123"

    def test_missing_pk_yields_none_not_the_string(self, store: MilvusVectorStore) -> None:
        hit = {"distance": 0.1, "entity": {"text": "t"}}
        (record,) = store._parse_search_response([[hit]])
        assert record["id"] is None

    def test_empty_response_is_empty_list(self, store: MilvusVectorStore) -> None:
        assert store._parse_search_response([]) == []
