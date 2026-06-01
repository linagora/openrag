"""End-to-end integration tests for :class:`MilvusVectorStore`.

These tests round-trip through a real Milvus 2.6 instance: they create a
fresh collection per test, exercise the public surface, and drop the
collection on teardown. They are gated by the ``integration`` pytest marker
and auto-skip when the configured Milvus host is not reachable.

Run locally against the dev compose stack:

    docker compose up -d milvus
    uv run pytest tests/integration/test_milvus_store_integration.py -m integration

Lives under ``tests/integration/`` per the Phase 13C target test layout
(``tests/{unit,integration,load}``) — see
``docs/refactoring/REFACTORING_STRATEGY_v1.md``. Pure-logic tests (filter
expressions, ID coercion, ABC discipline) stay colocated at
``openrag/services/storage/test_milvus_store.py`` until the Phase 13C sweep
relocates them under ``tests/unit/``.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator

import pytest

from openrag.core.config.infrastructure import VectorDBConfig
from openrag.core.models.chunk import Chunk, ChunkType
from openrag.services.storage.milvus_store import MilvusVectorStore

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Reachability gate — keeps the suite green when Milvus isn't running
# ---------------------------------------------------------------------------


def _milvus_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Embedding dimension is intentionally tiny — smaller = faster index build,
# and the schema cares about *having* a dimension, not the specific value.
_EMBEDDING_DIM = 4


def _embedding(seed: float) -> list[float]:
    """Build a small deterministic vector. Same seed = same vector."""
    return [seed, seed + 0.1, seed + 0.2, seed + 0.3]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def milvus_host_port() -> tuple[str, int]:
    """Resolve the Milvus endpoint, preferring test-specific env overrides.

    Defaults to ``localhost:19530`` because the test runs on the host, not
    inside the docker network where the service is named ``milvus``.

    ``VDB_HOST`` / ``MILVUS_HOST`` are NOT honoured here because pymilvus
    auto-loads the project's ``.env`` at import time (see ``pymilvus.settings``),
    which would inject the docker-network hostname ``milvus`` into a host-side
    test run. The dedicated ``OPENRAG_TEST_VDB_HOST`` env keeps the runtime
    config and the test config independent.
    """
    host = os.getenv("OPENRAG_TEST_VDB_HOST", "localhost")
    port = int(os.getenv("OPENRAG_TEST_VDB_PORT", "19530"))
    return host, port


@pytest.fixture(scope="module")
def _live_milvus(milvus_host_port: tuple[str, int]) -> None:
    host, port = milvus_host_port
    if not _milvus_reachable(host, port):
        pytest.skip(f"Milvus not reachable at {host}:{port} — skipping integration tests")


@pytest.fixture
def collection_name() -> str:
    """A throwaway collection name per test, so parallel runs don't collide."""
    # Milvus collection names are alphanumeric/underscore; uuid hex fits.
    return f"itest_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def hybrid_config(
    milvus_host_port: tuple[str, int],
    collection_name: str,
) -> VectorDBConfig:
    host, port = milvus_host_port
    return VectorDBConfig(
        host=host,
        port=port,
        collection_name=collection_name,
        hybrid_search=True,
        schema_version=1,
    )


@pytest.fixture
def dense_only_config(
    milvus_host_port: tuple[str, int],
    collection_name: str,
) -> VectorDBConfig:
    host, port = milvus_host_port
    return VectorDBConfig(
        host=host,
        port=port,
        collection_name=collection_name,
        hybrid_search=False,
        schema_version=1,
    )


@pytest.fixture
def hybrid_store(
    _live_milvus: None,
    hybrid_config: VectorDBConfig,
) -> Iterator[MilvusVectorStore]:
    """A real hybrid-enabled store wired to a freshly-named collection.

    The collection is created lazily by ``initialize()`` and dropped after
    every test so suite reruns don't accumulate orphaned collections.
    """
    store = MilvusVectorStore(hybrid_config)
    try:
        yield store
    finally:
        # Best-effort teardown — collection may not exist if a test never
        # initialized it (or already dropped it explicitly).
        try:
            if store._client.has_collection(hybrid_config.collection_name):
                store._client.drop_collection(hybrid_config.collection_name)
        except Exception:
            pass


@pytest.fixture
def dense_only_store(
    _live_milvus: None,
    dense_only_config: VectorDBConfig,
) -> Iterator[MilvusVectorStore]:
    """A real dense-only store (no ``sparse`` field) on a fresh collection.

    Mirrors :func:`hybrid_store` but with ``hybrid_search=False`` so
    ``search()`` exercises the dense dispatch branch end-to-end.
    """
    store = MilvusVectorStore(dense_only_config)
    try:
        yield store
    finally:
        try:
            if store._client.has_collection(dense_only_config.collection_name):
                store._client.drop_collection(dense_only_config.collection_name)
        except Exception:
            pass


def _chunk(text: str, partition: str, seed: float, **extra) -> Chunk:
    """Build a freshly-embedded chunk with sensible defaults."""
    return Chunk(
        text=text,
        document_id=extra.pop("document_id", "doc-1"),
        partition=partition,
        embedding=_embedding(seed),
        chunk_type=ChunkType.TEXT,
        metadata=extra,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Happy-path round trip: create → upsert → search → query → delete → drop."""

    @pytest.mark.asyncio
    async def test_initialize_creates_collection(
        self, hybrid_store: MilvusVectorStore, hybrid_config: VectorDBConfig
    ) -> None:
        assert await hybrid_store.collection_exists(hybrid_config.collection_name) is False
        await hybrid_store.initialize(_EMBEDDING_DIM)
        assert await hybrid_store.collection_exists(hybrid_config.collection_name) is True

    @pytest.mark.asyncio
    async def test_initialize_is_idempotent(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        # Second call must not raise and must not re-create.
        await hybrid_store.initialize(_EMBEDDING_DIM)
        assert hybrid_store._loaded is True

    @pytest.mark.asyncio
    async def test_ensure_collection_rejects_dimension_change(
        self, hybrid_store: MilvusVectorStore, hybrid_config: VectorDBConfig
    ) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        with pytest.raises(ValueError, match="Drop the collection before re-sizing"):
            await hybrid_store.ensure_collection(hybrid_config.collection_name, _EMBEDDING_DIM + 1)

    @pytest.mark.asyncio
    async def test_upsert_returns_insert_count(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        chunks = [
            _chunk("alpha doc one", "p1", 0.1),
            _chunk("beta doc two", "p1", 0.2),
            _chunk("gamma doc three", "p1", 0.3),
        ]
        n = await hybrid_store.upsert(chunks)
        assert n == 3

    @pytest.mark.asyncio
    async def test_upsert_without_embedding_raises(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        bad = Chunk(text="missing", partition="p1", embedding=None)
        from openrag.core.utils.exceptions import VDBInsertError

        with pytest.raises(VDBInsertError, match="no embedding"):
            await hybrid_store.upsert([bad])

    @pytest.mark.asyncio
    async def test_dense_search_returns_results(self, dense_only_store: MilvusVectorStore) -> None:
        await dense_only_store.initialize(_EMBEDDING_DIM)
        chunks = [
            _chunk("alpha", "p1", 0.1),
            _chunk("beta", "p1", 0.5),
            _chunk("gamma", "p1", 0.9),
        ]
        await dense_only_store.upsert(chunks)
        # Force the collection to flush so reads see the writes — Milvus is
        # eventually consistent in default mode but our config sets Strong
        # consistency so the search below should see everything.
        hits = await dense_only_store.search(_embedding(0.1), top_k=10)
        assert len(hits) >= 1
        for hit in hits:
            assert "id" in hit
            assert "score" in hit
            assert "vector" not in hit, "raw vector must be stripped from results"

    @pytest.mark.asyncio
    async def test_search_with_partition_filter(self, dense_only_store: MilvusVectorStore) -> None:
        await dense_only_store.initialize(_EMBEDDING_DIM)
        await dense_only_store.upsert(
            [
                _chunk("a", "p1", 0.1),
                _chunk("b", "p1", 0.2),
                _chunk("c", "p2", 0.3),
            ]
        )
        hits = await dense_only_store.search(_embedding(0.1), top_k=10, filters={"partition": "p1"})
        assert len(hits) >= 1
        for hit in hits:
            assert hit["partition"] == "p1"


class TestHybridSearch:
    @pytest.mark.asyncio
    async def test_hybrid_search_returns_fused_results(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        await hybrid_store.upsert(
            [
                _chunk("milvus vector database", "p1", 0.1),
                _chunk("postgres relational database", "p1", 0.5),
                _chunk("redis key value store", "p1", 0.9),
            ]
        )
        hits = await hybrid_store.search(
            _embedding(0.1),
            query_text="milvus database",
            top_k=5,
        )
        assert len(hits) >= 1
        # RRF fusion still returns the same shape — id, score, entity fields.
        for hit in hits:
            assert "id" in hit
            assert "score" in hit
            assert "text" in hit


class TestDeleteByFilter:
    @pytest.mark.asyncio
    async def test_delete_by_partition(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        await hybrid_store.upsert(
            [
                _chunk("a", "p1", 0.1),
                _chunk("b", "p2", 0.2),
            ]
        )
        deleted = await hybrid_store.delete_by_filter({"partition": "p1"})
        # We don't assert an exact count — Milvus returns delete_count, but
        # the integration's value is that the call succeeds and p1 vanishes.
        assert deleted >= 0
        remaining_p1 = await hybrid_store.query_ids_by_filter(hybrid_store._collection_name, {"partition": "p1"})
        assert remaining_p1 == []

    @pytest.mark.asyncio
    async def test_delete_by_filter_with_wildcard_partition_raises(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        with pytest.raises(ValueError, match="drop_collection"):
            await hybrid_store.delete_by_filter({"partition": "all"})


class TestQueryByFilter:
    @pytest.mark.asyncio
    async def test_query_ids_returns_string_ids(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        await hybrid_store.upsert([_chunk("only", "p1", 0.1)])
        ids = await hybrid_store.query_ids_by_filter(hybrid_store._collection_name, {"partition": "p1"})
        assert ids, "expected at least one row matching partition=p1"
        for chunk_id in ids:
            assert isinstance(chunk_id, str)
            assert chunk_id.isdigit(), f"Milvus _id round-trip lost INT64 form: {chunk_id}"

    @pytest.mark.asyncio
    async def test_query_chunks_returns_full_records(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        await hybrid_store.upsert([_chunk("only", "p1", 0.1)])
        rows = await hybrid_store.query_chunks_by_filter(hybrid_store._collection_name, {"partition": "p1"})
        assert rows
        assert rows[0]["partition"] == "p1"
        assert rows[0]["text"] == "only"
        # NOTE: ``_iter_query`` does NOT strip the vector field, unlike the
        # search path (which filters via ``_SEARCH_RESULT_DROPPED_KEYS``).
        # ``query_chunks_by_filter`` therefore leaks the dense vector when
        # called with the default ``["*"]`` output_fields — asymmetric with
        # ``search()`` and contradicts the method docstring. Tracked as a
        # follow-up; this test documents current behaviour so the next change
        # is intentional.


class TestDropAndDelete:
    @pytest.mark.asyncio
    async def test_drop_collection_lets_initialize_recreate(
        self, hybrid_store: MilvusVectorStore, hybrid_config: VectorDBConfig
    ) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        await hybrid_store.drop_collection(hybrid_config.collection_name)
        assert await hybrid_store.collection_exists(hybrid_config.collection_name) is False
        # After drop, the store is allowed to re-initialize from scratch —
        # otherwise per-tenant lifecycles would need a new instance just to
        # rebuild the collection.
        await hybrid_store.initialize(_EMBEDDING_DIM)
        assert await hybrid_store.collection_exists(hybrid_config.collection_name) is True

    @pytest.mark.asyncio
    async def test_delete_by_id_removes_rows(self, hybrid_store: MilvusVectorStore) -> None:
        await hybrid_store.initialize(_EMBEDDING_DIM)
        await hybrid_store.upsert([_chunk("to-delete", "p1", 0.1)])
        ids = await hybrid_store.query_ids_by_filter(hybrid_store._collection_name, {"partition": "p1"})
        assert ids, "expected the upsert to land at least one row"
        deleted = await hybrid_store.delete(ids)
        assert deleted >= 0
        remaining = await hybrid_store.query_ids_by_filter(hybrid_store._collection_name, {"partition": "p1"})
        assert remaining == []
