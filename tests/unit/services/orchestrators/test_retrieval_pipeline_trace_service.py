from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.chunk import Chunk
from core.models.preset import PartitionConfig
from core.models.query import Query, SearchQueries
from core.retrieval.trace import RetrievalTraceBuilder
from services.orchestrators.retrieval_service import RetrievalService


def _chunk(identifier: str) -> Chunk:
    return Chunk(id=identifier, text=identifier, metadata={"_id": identifier})


class _Searcher:
    def __init__(self, results=None):
        self.results = results or [_chunk("a"), _chunk("b")]
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.results)

    async def multi_query_search(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.results)

    async def get_related_chunks(self, **_kwargs):
        return []

    async def get_ancestor_chunks(self, **_kwargs):
        return []


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        retriever=SimpleNamespace(
            type="single",
            top_k=6,
            similarity_threshold=0.5,
            with_surrounding_chunks=False,
            include_related=False,
            include_ancestors=False,
            related_limit=10,
            max_ancestor_depth=None,
            allow_filterless_fallback=True,
            k_queries=3,
            combine=False,
            max_partition_concurrency=16,
        ),
        reranker=SimpleNamespace(enabled=False, top_k=5),
        partitions={},
        models=SimpleNamespace(embedder={}, reranker={}, llm={}),
        vectordb=SimpleNamespace(hybrid_search=True),
    )


def _partition(name: str, embedder: str) -> PartitionConfig:
    return PartitionConfig(
        name=name,
        embedder=embedder,
        indexation=IndexationPipelineConfig(),
        retrieval=RetrievalPipelineConfig(),
    )


def _service(searcher=None, *, config=None, searcher_factory=None) -> RetrievalService:
    return RetrievalService(
        searcher=searcher or _Searcher(),
        reranker=None,
        llm=None,
        config=config or _config(),
        searcher_factory=searcher_factory,
    )


@pytest.mark.asyncio
async def test_retrieve_threads_trace_and_records_final_stage():
    searcher = _Searcher()
    trace = RetrievalTraceBuilder("request-1", "hello")

    chunks = await _service(searcher).retrieve(
        partitions=["tenant-a"],
        query=Query(query="hello"),
        trace=trace,
    )

    assert [chunk.id for chunk in chunks] == ["a", "b"]
    assert searcher.calls[0]["trace"] is trace
    assert [candidate.id for candidate in trace.stages["final"].candidates] == ["a", "b"]


def test_partition_fusion_records_duplicates_scores_and_final_cutoff():
    first_b = _chunk("b")
    duplicate_b = _chunk("b")
    trace = RetrievalTraceBuilder("request-1", "question")

    fused = RetrievalService.fuse(
        [[_chunk("a"), first_b], [duplicate_b, _chunk("c")]],
        top_k=2,
        trace=trace,
    )

    assert [chunk.id for chunk in fused] == ["b", "a"]
    partition_fused = trace.stages["partition_fused"].candidates
    duplicate = next(candidate for candidate in partition_fused if candidate.duplicate_of is not None)
    assert duplicate.id == "b"
    assert duplicate.scores["fused"] == pytest.approx(1 / 62 + 1 / 61)
    removed = next(candidate for candidate in partition_fused if candidate.id == "c")
    assert removed.removal_reason.code == "final_top_n"


@pytest.mark.asyncio
async def test_multi_partition_trace_preserves_partition_and_nested_query_identity():
    config = _config()
    config.partitions = {
        "a": _partition("a", "embed-a"),
        "b": _partition("b", "embed-b"),
    }
    searchers = {"embed-a": _Searcher([_chunk("a-hit")]), "embed-b": _Searcher([_chunk("b-hit")])}
    trace = RetrievalTraceBuilder("request-1", "original question")

    await _service(
        config=config,
        searcher_factory=lambda name: searchers[name],
    ).retrieve_multi(
        partitions=["a", "b"],
        search_queries=SearchQueries(query_list=[Query(query="rewrite one"), Query(query="rewrite two")]),
        trace=trace,
    )

    assert [child.partition for child in trace.query_traces] == ["a", "b"]
    assert [[nested.query for nested in child.query_traces] for child in trace.query_traces] == [
        ["rewrite one", "rewrite two"],
        ["rewrite one", "rewrite two"],
    ]
    assert trace.stages["pre_rerank"].status == "unavailable"


@pytest.mark.asyncio
async def test_single_query_multi_partition_trace_uses_effective_query():
    config = _config()
    config.partitions = {
        "a": _partition("a", "embed-a"),
        "b": _partition("b", "embed-b"),
    }
    searchers = {"embed-a": _Searcher(), "embed-b": _Searcher()}
    trace = RetrievalTraceBuilder("request-1", "original follow-up")

    await _service(
        config=config,
        searcher_factory=lambda name: searchers[name],
    ).retrieve(
        partitions=["a", "b"],
        query=Query(query="contextualized question"),
        trace=trace,
    )

    assert [(child.partition, child.query) for child in trace.query_traces] == [
        ("a", "contextualized question"),
        ("b", "contextualized question"),
    ]
