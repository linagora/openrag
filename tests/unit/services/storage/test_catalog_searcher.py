from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from core.models.chunk import Chunk
from services.storage.catalog_searcher import CatalogSearcher
from services.storage.vector_store_searcher import VectorStoreSearcher


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("search", {"query": "q", "partition": ["a", "b"], "top_k": 4}),
        ("multi_query_search", {"queries": ["q"], "partition": ["a", "b"], "top_k_per_query": 4}),
        ("get_related_chunks", {"partition": "a", "relationship_id": "r", "limit": 4}),
        ("get_ancestor_chunks", {"partition": "a", "file_id": "f", "limit": 4}),
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
