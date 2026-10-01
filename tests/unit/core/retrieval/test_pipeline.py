"""Tests for RetrieverPipeline using fake Retriever / Reranker."""

from __future__ import annotations

import pytest
from core.models.chunk import Chunk
from core.models.query import Query, SearchQueries, TemporalPredicate
from core.models.retrieval_result import ScoredChunk
from core.retrieval.pipeline import RetrieverPipeline
from core.retrieval.retriever import Retriever


class FakeRetriever(Retriever):
    """Plays back canned per-call results; records call kwargs."""

    def __init__(self, expansion_enabled: bool = False) -> None:
        self.calls: list[dict] = []
        self.results_queue: list[list[Chunk]] = []
        self.expand_input: list[Chunk] | None = None
        self.expand_result: list[Chunk] | None = None
        self.expand_filter_params: dict | None = None
        self.expansion_enabled = expansion_enabled

    async def retrieve(self, partition, query, filter=None, filter_params=None):
        self.calls.append({"partition": partition, "query": query, "filter": filter, "filter_params": filter_params})
        if self.results_queue:
            return self.results_queue.pop(0)
        return []

    async def expand_search_results(self, results, filter_params=None):
        self.expand_input = list(results)
        self.expand_filter_params = filter_params
        return list(self.expand_result) if self.expand_result is not None else list(results)


class FakeReranker:
    """Reverses input ordering — easy to detect in assertions."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def rerank(self, query, documents, top_k=None):
        self.calls.append({"query": query, "documents": list(documents), "top_k": top_k})
        # Reverse ranking, perfect score for the (now-)first item
        return [(i, float(len(documents) - i)) for i in range(len(documents) - 1, -1, -1)]


def _chunks(*ids: str) -> list[Chunk]:
    return [Chunk(id=i, text=f"text-{i}", partition="p1") for i in ids]


@pytest.mark.asyncio
async def test_retrieve_docs_no_filter_no_rerank_no_expand():
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b", "c")]
    p = RetrieverPipeline(retriever=r)
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"))
    assert [c.id for c in out] == ["a", "b", "c"]
    assert r.calls[0]["filter"] is None


@pytest.mark.asyncio
async def test_retrieve_docs_temporal_filter_passed_through():
    r = FakeRetriever()
    r.results_queue = [_chunks("a")]
    p = RetrieverPipeline(retriever=r)
    q = Query(
        query="hi",
        temporal_filters=[TemporalPredicate(operator=">=", value="2026-01-01T00:00:00+00:00")],
    )
    await p.retrieve_docs(partition=["p1"], query=q)
    assert "created_at" in r.calls[0]["filter"]


@pytest.mark.asyncio
async def test_retrieve_docs_filterless_fallback_when_filter_returns_zero():
    r = FakeRetriever()
    r.results_queue = [[], _chunks("a")]
    p = RetrieverPipeline(retriever=r, allow_filterless_fallback=True)
    q = Query(
        query="hi",
        temporal_filters=[TemporalPredicate(operator=">=", value="2026-01-01T00:00:00+00:00")],
    )
    out = await p.retrieve_docs(partition=["p1"], query=q)
    assert [c.id for c in out] == ["a"]
    assert r.calls[0]["filter"] is not None
    assert r.calls[1]["filter"] is None


@pytest.mark.asyncio
async def test_retrieve_docs_no_fallback_when_disabled():
    r = FakeRetriever()
    r.results_queue = [[]]
    p = RetrieverPipeline(retriever=r, allow_filterless_fallback=False)
    q = Query(
        query="hi",
        temporal_filters=[TemporalPredicate(operator=">=", value="2026-01-01T00:00:00+00:00")],
    )
    out = await p.retrieve_docs(partition=["p1"], query=q)
    assert out == []
    assert len(r.calls) == 1


@pytest.mark.asyncio
async def test_retrieve_docs_runs_reranker_when_enabled():
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b", "c")]
    rer = FakeReranker()
    p = RetrieverPipeline(retriever=r, reranker=rer)
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"))
    assert [c.id for c in out] == ["c", "b", "a"]
    assert rer.calls[0]["query"] == "hi"


class ScoringReranker:
    """Ranks c > a > b, with scores deliberately unrelated to the ordering so a
    mixed-up index/score pairing can't pass by coincidence."""

    async def rerank(self, query, documents, top_k=None):
        return [(2, 0.91), (0, 0.42), (1, 0.07)]


@pytest.mark.asyncio
async def test_reranked_chunks_come_back_as_scored_chunks():
    """The reranker's score survives on the chunk as a typed field, and the
    chunks stay ``Chunk`` instances so every downstream signature holds."""
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b", "c")]
    p = RetrieverPipeline(retriever=r, reranker=ScoringReranker())
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"))

    assert [(c.id, c.rerank_score) for c in out] == [("c", 0.91), ("a", 0.42), ("b", 0.07)]
    assert all(isinstance(c, ScoredChunk) for c in out)
    assert all(isinstance(c, Chunk) for c in out)


@pytest.mark.asyncio
async def test_rerank_score_reaches_the_langchain_metadata():
    """``to_langchain`` is the boundary the API response is built from, so the
    score has to survive that conversion -- and the scores that never ran must
    not appear there as nulls."""
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b", "c")]
    p = RetrieverPipeline(retriever=r, reranker=ScoringReranker())
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"))

    metadata = out[0].to_langchain().metadata
    assert metadata["rerank_score"] == 0.91
    assert "vector_score" not in metadata
    assert "combined_score" not in metadata


@pytest.mark.asyncio
async def test_no_score_at_all_when_reranker_disabled():
    """No reranker means plain ``Chunk``s with no score -- not a null, and not a
    0.0 that reads like a real score the chunk was ranked with."""
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b")]
    p = RetrieverPipeline(retriever=r)
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"))

    assert not any(isinstance(c, ScoredChunk) for c in out)
    assert all("rerank_score" not in c.to_langchain().metadata for c in out)


@pytest.mark.asyncio
async def test_reranking_does_not_mutate_the_retriever_s_chunks():
    """The score lands on a new object. The same chunk is reranked twice on the
    expansion path and, on the multi-query path, once per sub-query against a
    *different* query -- mutating in place would let one sub-query's score bleed
    into another's list."""
    original = _chunks("a", "b")
    r = FakeRetriever()
    r.results_queue = [original]
    p = RetrieverPipeline(retriever=r, reranker=FakeReranker())
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"))

    assert all(c.rerank_score is not None for c in out)
    assert all(not isinstance(c, ScoredChunk) for c in original)


@pytest.mark.asyncio
async def test_re_reranking_replaces_the_score_without_nesting():
    """Expansion reranks an already-scored chunk. The second score must replace
    the first, not wrap it in another ScoredChunk layer."""
    scored = ScoredChunk.from_chunk(_chunks("a")[0], rerank_score=0.11, vector_score=0.5)
    again = ScoredChunk.from_chunk(scored, rerank_score=0.99)

    assert again.rerank_score == 0.99
    assert again.vector_score == 0.5  # untouched scores survive
    assert scored.rerank_score == 0.11  # the original is left alone


@pytest.mark.asyncio
async def test_retrieve_docs_expansion_path_re_reranks():
    r = FakeRetriever(expansion_enabled=True)
    r.results_queue = [_chunks("a", "b")]
    r.expand_result = _chunks("a", "b", "c")
    rer = FakeReranker()
    p = RetrieverPipeline(retriever=r, reranker=rer, reranker_top_k=2)
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"))
    # Two reranker invocations: pre-expansion (2 chunks), post-expansion (3 chunks)
    assert len(rer.calls) == 2
    assert len(rer.calls[0]["documents"]) == 2
    assert len(rer.calls[1]["documents"]) == 3
    assert {c.id for c in out} == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_get_relevant_docs_runs_one_call_per_subquery_and_fuses():
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b"), _chunks("b", "c")]
    p = RetrieverPipeline(retriever=r)
    sq = SearchQueries(query_list=[Query(query="q1"), Query(query="q2")])
    out = await p.get_relevant_docs(partition=["p1"], search_queries=sq)
    assert len(r.calls) == 2
    assert {c.id for c in out} == {"a", "b", "c"}
    # 'b' appears in both lists -> highest fused score
    assert out[0].id == "b"


@pytest.mark.asyncio
async def test_get_relevant_docs_applies_top_k_cap():
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b", "c")]
    p = RetrieverPipeline(retriever=r)
    sq = SearchQueries(query_list=[Query(query="q1")])
    out = await p.get_relevant_docs(partition=["p1"], search_queries=sq, top_k=2)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_retrieve_docs_expansion_no_new_chunks_skips_second_rerank():
    r = FakeRetriever(expansion_enabled=True)
    r.results_queue = [_chunks("a", "b")]
    r.expand_result = _chunks("a", "b")  # expansion returns same set
    rer = FakeReranker()
    p = RetrieverPipeline(retriever=r, reranker=rer, reranker_top_k=2)
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"))
    # Only the pre-expansion rerank fired.
    assert len(rer.calls) == 1
    assert {c.id for c in out} == {"a", "b"}


@pytest.mark.asyncio
async def test_rerank_chunks_short_circuits_on_empty_input():
    """Direct cover of the early-return guard inside _rerank_chunks."""
    from core.retrieval.pipeline import _rerank_chunks

    rer = FakeReranker()
    out = await _rerank_chunks(rer, "q", [])
    assert out == []
    assert rer.calls == []


def test_pipeline_expansion_enabled_false_for_non_base_retriever():
    """Retrievers without an expansion_enabled attr (e.g. custom impls) are
    treated as non-expanding via getattr default."""

    class MinimalRetriever(Retriever):
        async def retrieve(self, partition, query, filter=None, filter_params=None):
            return []

        async def expand_search_results(self, results):
            return results

    p = RetrieverPipeline(retriever=MinimalRetriever())
    assert p.expansion_enabled is False


@pytest.mark.asyncio
async def test_retrieve_docs_caps_to_top_k():
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b", "c", "d")]
    p = RetrieverPipeline(retriever=r)
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"), top_k=2)
    assert [c.id for c in out] == ["a", "b"]


@pytest.mark.asyncio
async def test_retrieve_docs_top_k_zero_returns_empty():
    """top_k=0 must mean "zero results", not "treated as None" (the legacy bug)."""
    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b")]
    p = RetrieverPipeline(retriever=r)
    out = await p.retrieve_docs(partition=["p1"], query=Query(query="hi"), top_k=0)
    assert out == []


@pytest.mark.asyncio
async def test_get_relevant_docs_passes_configured_rrf_k(monkeypatch):
    """The per-partition rrf_k must reach rrf_reranking, not the hardcoded 60 (#707)."""
    import core.retrieval.pipeline as pipeline_mod

    captured = {}
    real = pipeline_mod.rrf_reranking

    def spy(ranked_lists, key_fn=None, k=60):
        captured["k"] = k
        return real(ranked_lists, key_fn=key_fn, k=k)

    monkeypatch.setattr(pipeline_mod, "rrf_reranking", spy)

    r = FakeRetriever()
    r.results_queue = [_chunks("a", "b"), _chunks("b", "c")]
    p = RetrieverPipeline(retriever=r, rrf_k=17)
    sq = SearchQueries(query_list=[Query(query="q1"), Query(query="q2")])
    await p.get_relevant_docs(partition=["p1"], search_queries=sq)

    assert captured["k"] == 17


def test_rrf_k_defaults_to_canonical_60():
    assert RetrieverPipeline(retriever=FakeRetriever()).rrf_k == 60


def test_persisted_score_in_metadata_never_masquerades_as_this_query_s_score():
    """A chunk can arrive carrying a ``rerank_score`` in its free-form metadata
    -- the collection has a dynamic field, so whatever a caller sent as upload
    metadata is persisted and read back. ``to_langchain`` must publish the typed
    field this retrieval set, not that one, in both directions: overwritten when
    a reranker ran, dropped when none did."""
    stale = {"rerank_score": 0.99, "vector_score": 0.99, "author": "alice"}

    scored = ScoredChunk(id="x", text="t", metadata=dict(stale), rerank_score=0.12)
    metadata = scored.to_langchain().metadata
    assert metadata["rerank_score"] == 0.12
    assert "vector_score" not in metadata
    assert metadata["author"] == "alice"

    unscored = ScoredChunk(id="x", text="t", metadata=dict(stale))
    assert "rerank_score" not in unscored.to_langchain().metadata
