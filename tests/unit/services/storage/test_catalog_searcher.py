from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from core.models.chunk import Chunk
from loguru import logger
from prometheus_client import REGISTRY
from services.storage import catalog_searcher
from services.storage.catalog_searcher import CatalogSearcher
from services.storage.vector_store_searcher import VectorStoreSearcher


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        (
            "search",
            {
                "query": "q",
                "partition": ["a", "b"],
                "top_k": 4,
                "filter": "page > 2",
                "filter_params": {"file_id": ["f"]},
                "similarity_threshold": 0.3,
                "with_surrounding_chunks": False,
            },
        ),
        (
            "multi_query_search",
            {
                "queries": ["q"],
                "partition": ["a", "b"],
                "top_k_per_query": 4,
                "filter": "page > 2",
                "filter_params": {"file_id": ["f"]},
                "similarity_threshold": 0.3,
                "with_surrounding_chunks": False,
            },
        ),
        ("get_related_chunks", {"partition": "a", "relationship_id": "r", "limit": 4, "allowed_file_ids": ["f"]}),
        (
            "get_ancestor_chunks",
            {"partition": "a", "file_id": "f", "limit": 4, "max_ancestor_depth": 2, "allowed_file_ids": ["f"]},
        ),
    ],
)
async def test_all_methods_drop_orphans_preserving_order_and_partition(method, kwargs):
    chunks = [
        Chunk(id="1", document_id="f", partition="a", text="live"),
        Chunk(id="2", document_id="f", partition="b", text="deleted in b"),
        Chunk(id="3", document_id="f", partition="a", text="live neighbour"),
        Chunk(id="4", document_id="gone", partition="a", text="deleted"),
    ]
    inner = AsyncMock()
    getattr(inner, method).return_value = chunks
    repo = AsyncMock()
    repo.get_indexed_documents.return_value = {("a", "f"): datetime.now(UTC)}
    searcher = CatalogSearcher(inner, repo)

    result = await getattr(searcher, method)(**kwargs)

    assert [c.id for c in result] == ["1", "3"]
    getattr(inner, method).assert_awaited_once_with(**kwargs)
    assert set(repo.get_indexed_documents.call_args.args[0]) == {("a", "f"), ("b", "f"), ("a", "gone")}


async def test_catalog_failure_propagates_and_deletions_are_not_cached():
    inner = AsyncMock()
    inner.search.return_value = [Chunk(id="1", document_id="f", partition="a", text="live")]
    repo = AsyncMock()
    repo.get_indexed_documents.side_effect = [{("a", "f"): datetime.now(UTC)}, {}, RuntimeError("catalog down")]
    searcher = CatalogSearcher(inner, repo)
    assert len(await searcher.search("q", ["a"], 5)) == 1
    assert await searcher.search("q", ["a"], 5) == []
    with pytest.raises(RuntimeError, match="catalog down"):
        await searcher.search("q", ["a"], 5)


async def test_empty_results_skip_catalog():
    inner = AsyncMock()
    inner.search.return_value = []
    repo = AsyncMock()
    assert await CatalogSearcher(inner, repo).search("q", ["a"], 5) == []
    repo.get_indexed_documents.assert_not_awaited()


@pytest.mark.parametrize("multi", [False, True])
async def test_surrounding_orphan_is_removed_after_real_search_expansion(multi):
    vectors = AsyncMock()
    vectors.search.return_value = [{"id": "1", "file_id": "live", "partition": "a", "next_section_id": 2}]
    vectors.query_chunks_by_filter.return_value = [{"_id": 2, "file_id": "deleted", "partition": "a"}]
    embedder = AsyncMock()
    embedder.embed.return_value = [[0.1]]
    repo = AsyncMock()
    repo.get_indexed_documents.return_value = {("a", "live"): datetime.now(UTC)}
    searcher = CatalogSearcher(VectorStoreSearcher(vectors, embedder, repo, "collection"), repo)
    if multi:
        chunks = await searcher.multi_query_search(["q"], ["a"], 5)
    else:
        chunks = await searcher.search("q", ["a"], 5)
    assert [c.id for c in chunks] == ["1"]
    assert set(repo.get_indexed_documents.call_args.args[0]) == {("a", "live"), ("a", "deleted")}


@pytest.fixture
def orphan_logs(monkeypatch):
    records = []
    now = [100.0]
    monkeypatch.setattr(catalog_searcher, "monotonic", lambda: now[0], raising=False)
    monkeypatch.setattr(catalog_searcher, "_next_orphan_warning_at", 0.0, raising=False)
    sink = logger.add(
        lambda message: records.append(message.record), filter=lambda r: r["name"] == catalog_searcher.__name__
    )
    try:
        yield records, now
    finally:
        logger.remove(sink)


async def test_orphan_warning_deduplicates_keys_and_bounds_payload(orphan_logs):
    records, _ = orphan_logs
    inner = AsyncMock()
    # Two chunks per missing file, with oversized IDs to exercise the log cap.
    inner.search.return_value = [
        Chunk(id=str(i), partition="p" * 1000, document_id=f"{i // 2}:" + "f" * 1000, text="private content")
        for i in range(100)
    ]
    repo = AsyncMock()
    repo.get_indexed_documents.return_value = {}
    assert await CatalogSearcher(inner, repo).search("q", ["p"], 100) == []
    assert len(records) == 1
    record = records[0]
    assert record["level"].name == "WARNING"
    extra = record["extra"]
    assert extra["dropped_chunks"] == 100
    assert extra["dropped_files"] == 50
    keys = extra["orphaned_files_sample"]
    assert len(keys) == 10
    assert len({(k["partition"], k["file_id"]) for k in keys}) == 10
    assert all(len(k["partition"]) <= 128 and len(k["file_id"]) <= 128 for k in keys)
    assert "private content" not in str(record)


async def test_log_throttle_is_shared_without_suppressing_checks_or_metric(orphan_logs):
    records, now = orphan_logs
    inner = AsyncMock()
    inner.search.return_value = [Chunk(id="1", partition="a", document_id="f", text="orphan")]
    repo = AsyncMock()
    repo.get_indexed_documents.return_value = {}
    first, second = CatalogSearcher(inner, repo), CatalogSearcher(inner, repo)
    metric = "openrag_retrieval_orphan_chunks_dropped_total"
    before = REGISTRY.get_sample_value(metric)
    assert await first.search("q", ["a"], 5) == []
    now[0] = 101.0
    assert await second.search("q", ["a"], 5) == []
    assert len(records) == 1
    now[0] = 160.0
    assert await second.search("q", ["a"], 5) == []
    assert len(records) == 2
    assert repo.get_indexed_documents.await_count == 3
    assert REGISTRY.get_sample_value(metric) - before == 3
    repo.get_indexed_documents.return_value = {("a", "f"): datetime.now(UTC)}
    now[0] = 220.0
    assert len(await first.search("q", ["a"], 5)) == 1
    assert len(records) == 2
    assert REGISTRY.get_sample_value(metric) - before == 3


async def test_unknown_search_argument_is_rejected_before_delegating():
    inner = AsyncMock()
    inner.search.return_value = []
    with pytest.raises(TypeError):
        await CatalogSearcher(inner, AsyncMock()).search("q", ["a"], 5, misspelled_filter={})
    inner.search.assert_not_awaited()
