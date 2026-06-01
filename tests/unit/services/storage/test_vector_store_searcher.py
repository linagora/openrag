"""Unit tests for :class:`VectorStoreSearcher`.

All I/O (VectorStore, Embedder, DocumentRepository) is mocked so tests run
without a live Milvus, vLLM, or database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from openrag.services.storage.vector_store_searcher import VectorStoreSearcher, _dict_to_chunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMBED_VEC = [0.1] * 8


def _make_row(id_: str, text: str = "hello", partition: str = "p1", **extra) -> dict:
    return {"id": id_, "text": text, "partition": partition, "file_id": "f1", **extra}


def _make_searcher(
    search_results=None,
    filter_results=None,
    file_ids_by_rel=None,
    ancestor_ids=None,
) -> tuple[VectorStoreSearcher, MagicMock, MagicMock, MagicMock]:
    store = MagicMock()
    store.search = AsyncMock(return_value=search_results or [])
    store.query_chunks_by_filter = AsyncMock(return_value=filter_results or [])

    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[_EMBED_VEC])

    doc_repo = MagicMock()
    doc_repo.get_file_ids_by_relationship = AsyncMock(return_value=file_ids_by_rel or [])
    doc_repo.get_ancestor_file_ids = AsyncMock(return_value=ancestor_ids or [])

    searcher = VectorStoreSearcher(
        vector_store=store,
        embedder=embedder,
        document_repo=doc_repo,
        collection="test_col",
    )
    return searcher, store, embedder, doc_repo


# ---------------------------------------------------------------------------
# _dict_to_chunk
# ---------------------------------------------------------------------------


def test_dict_to_chunk_uses_id_field():
    row = {"id": "abc", "text": "t", "partition": "p", "file_id": "f"}
    c = _dict_to_chunk(row)
    assert c.id == "abc"


def test_dict_to_chunk_uses_underscore_id_as_fallback():
    row = {"_id": 42, "text": "t", "partition": "p", "file_id": "f"}
    c = _dict_to_chunk(row)
    assert c.id == "42"


def test_dict_to_chunk_metadata_excludes_reserved_keys():
    row = {"id": "x", "text": "t", "partition": "p", "file_id": "f", "score": 0.9, "extra_key": "val"}
    c = _dict_to_chunk(row)
    assert "score" not in c.metadata
    assert "extra_key" in c.metadata


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_embeds_query_and_calls_store():
    searcher, store, embedder, _ = _make_searcher(
        search_results=[_make_row("1")],
    )
    await searcher.search(query="hello", partition=["p1"], top_k=5, with_surrounding_chunks=False)

    embedder.embed.assert_awaited_once_with(["hello"])
    store.search.assert_awaited_once()
    call_kwargs = store.search.call_args.kwargs
    assert call_kwargs["embedding"] == _EMBED_VEC
    assert call_kwargs["query_text"] == "hello"
    assert call_kwargs["top_k"] == 5
    assert call_kwargs["filters"]["partition"] == ["p1"]


@pytest.mark.asyncio
async def test_search_passes_filter_expr():
    searcher, store, *_ = _make_searcher(search_results=[])
    await searcher.search(
        query="q",
        partition=["p1"],
        top_k=3,
        filter="file_id == 'x'",
        with_surrounding_chunks=False,
    )
    call_kwargs = store.search.call_args.kwargs
    assert call_kwargs["filters"]["expr"] == "file_id == 'x'"


@pytest.mark.asyncio
async def test_search_with_surrounding_chunks_deduplicates():
    main_row = _make_row("1", prev_section_id="s0", next_section_id="s2")
    surrounding_rows = [_make_row("0"), _make_row("1")]  # "1" is a duplicate
    searcher, store, _, _ = _make_searcher(
        search_results=[main_row],
        filter_results=surrounding_rows,
    )
    chunks = await searcher.search(query="q", partition=["p1"], top_k=5)

    ids = [c.id for c in chunks]
    assert ids.count("1") == 1
    assert "0" in ids


@pytest.mark.asyncio
async def test_search_skips_surrounding_when_disabled():
    searcher, store, _, _ = _make_searcher(search_results=[_make_row("1")])
    await searcher.search(query="q", partition=["p1"], top_k=5, with_surrounding_chunks=False)
    store.query_chunks_by_filter.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_skips_surrounding_when_no_results():
    searcher, store, _, _ = _make_searcher(search_results=[])
    await searcher.search(query="q", partition=["p1"], top_k=5, with_surrounding_chunks=True)
    store.query_chunks_by_filter.assert_not_awaited()


# ---------------------------------------------------------------------------
# multi_query_search()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_query_search_embeds_all_queries():
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[_EMBED_VEC, _EMBED_VEC])

    store = MagicMock()
    store.search = AsyncMock(return_value=[])
    store.query_chunks_by_filter = AsyncMock(return_value=[])

    searcher = VectorStoreSearcher(
        vector_store=store,
        embedder=embedder,
        document_repo=MagicMock(),
        collection="col",
    )
    await searcher.multi_query_search(
        queries=["q1", "q2"], partition=["p1"], top_k_per_query=3, with_surrounding_chunks=False
    )
    embedder.embed.assert_awaited_once_with(["q1", "q2"])


@pytest.mark.asyncio
async def test_multi_query_search_deduplicates_across_queries():
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[_EMBED_VEC, _EMBED_VEC])

    # Both queries return the same chunk "1" plus a unique one each
    store = MagicMock()
    store.search = AsyncMock(
        side_effect=[
            [_make_row("1"), _make_row("2")],
            [_make_row("1"), _make_row("3")],
        ]
    )
    store.query_chunks_by_filter = AsyncMock(return_value=[])

    searcher = VectorStoreSearcher(
        vector_store=store,
        embedder=embedder,
        document_repo=MagicMock(),
        collection="col",
    )
    chunks = await searcher.multi_query_search(
        queries=["q1", "q2"], partition=["p1"], top_k_per_query=5, with_surrounding_chunks=False
    )
    ids = [c.id for c in chunks]
    assert ids.count("1") == 1
    assert sorted(ids) == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# get_related_chunks()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_related_chunks_returns_empty_when_no_file_ids():
    searcher, store, _, doc_repo = _make_searcher(file_ids_by_rel=[])
    result = await searcher.get_related_chunks(partition="p1", relationship_id="r1", limit=10)
    assert result == []
    store.query_chunks_by_filter.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_related_chunks_queries_store_with_file_ids():
    rows = [_make_row("1"), _make_row("2"), _make_row("3")]
    searcher, store, _, doc_repo = _make_searcher(
        file_ids_by_rel=["f1", "f2"],
        filter_results=rows,
    )
    chunks = await searcher.get_related_chunks(partition="p1", relationship_id="r1", limit=2)
    assert len(chunks) == 2
    doc_repo.get_file_ids_by_relationship.assert_awaited_once_with(partition="p1", relationship_id="r1")
    call_args = store.query_chunks_by_filter.call_args
    assert call_args.args[1]["file_id"] == ["f1", "f2"]


# ---------------------------------------------------------------------------
# get_ancestor_chunks()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ancestor_chunks_returns_empty_when_no_ancestors():
    searcher, store, _, _ = _make_searcher(ancestor_ids=[])
    result = await searcher.get_ancestor_chunks(partition="p1", file_id="f1", limit=5)
    assert result == []
    store.query_chunks_by_filter.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_ancestor_chunks_applies_limit():
    rows = [_make_row(str(i)) for i in range(10)]
    searcher, store, _, doc_repo = _make_searcher(
        ancestor_ids=["a1", "a2"],
        filter_results=rows,
    )
    chunks = await searcher.get_ancestor_chunks(partition="p1", file_id="f1", limit=4)
    assert len(chunks) == 4


@pytest.mark.asyncio
async def test_get_ancestor_chunks_passes_max_depth():
    searcher, _, _, doc_repo = _make_searcher(ancestor_ids=[])
    await searcher.get_ancestor_chunks(partition="p1", file_id="f1", limit=5, max_ancestor_depth=2)
    doc_repo.get_ancestor_file_ids.assert_awaited_once_with(partition="p1", file_id="f1", max_ancestor_depth=2)
