"""Unit tests for :class:`RetrievalService` (Phase 8C.1).

The Ray-backed searcher is faked (the service is constructed with a
``RetrievalSearcher`` stub, exactly as the container will inject
``MilvusRayShim``). Default config uses the ``single`` retriever with
the reranker disabled, so the core pipeline path is exercised end-to-end
without inference services.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.chunk import Chunk
from core.models.preset import PartitionConfig
from core.models.query import Query, SearchQueries
from core.utils.exceptions import PartitionNotFoundError
from services.orchestrators.retrieval_service import RetrievalService


def _chunk(cid: str, text: str = "t") -> Chunk:
    return Chunk(id=cid, text=text, metadata={"_id": cid})


class FakeSearcher:
    def __init__(self):
        self.search_calls: list[dict] = []
        self.search_result: list[Chunk] = []
        self.related_result: list[Chunk] = []
        self.ancestor_result: list[Chunk] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return list(self.search_result)

    async def multi_query_search(self, **kwargs):
        return list(self.search_result)

    async def get_related_chunks(self, **kwargs):
        return list(self.related_result)

    async def get_ancestor_chunks(self, **kwargs):
        return list(self.ancestor_result)


def _config(rtype: str = "single", reranker_enabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        retriever=SimpleNamespace(
            type=rtype,
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
        ),
        reranker=SimpleNamespace(enabled=reranker_enabled, top_k=5),
        partitions={},
    )


def _partition(
    *,
    name: str = "tenant-a",
    embedder: str = "embed-a",
    retrieval: RetrievalPipelineConfig | None = None,
) -> PartitionConfig:
    return PartitionConfig(
        name=name,
        embedder=embedder,
        indexation=IndexationPipelineConfig(),
        retrieval=retrieval or RetrievalPipelineConfig(),
    )


class FakeReranker:
    def __init__(self):
        self.calls: list[dict] = []

    async def rerank(self, *, query, documents, top_k):
        self.calls.append({"query": query, "documents": documents, "top_k": top_k})
        return [(idx, 1.0) for idx in range(len(documents))]


def _svc(searcher, *, rtype="single", reranker_enabled=False) -> RetrievalService:
    return RetrievalService(
        searcher=searcher,
        reranker=None,
        llm=None,
        config=_config(rtype, reranker_enabled),
    )


# --------------------------------------------------------------------------- #
# search() — powers routers/search.py
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_normalizes_str_partition_and_passes_params():
    s = FakeSearcher()
    s.search_result = [_chunk("1"), _chunk("2")]
    out = await _svc(s).search(
        text="hello",
        partitions="p1",
        top_k=7,
        similarity_threshold=0.8,
        filter="file_id == 'x'",
        filter_params={"a": 1},
    )
    assert [c.id for c in out] == ["1", "2"]
    call = s.search_calls[0]
    assert call["partition"] == ["p1"]  # str normalized to list
    assert call["query"] == "hello"
    assert call["top_k"] == 7
    assert call["similarity_threshold"] == 0.8
    assert call["filter"] == "file_id == 'x'"
    assert call["filter_params"] == {"a": 1}
    assert call["with_surrounding_chunks"] is True


@pytest.mark.asyncio
async def test_search_no_expansion_when_flags_off():
    s = FakeSearcher()
    s.search_result = [_chunk("1")]
    s.related_result = [_chunk("rel")]
    out = await _svc(s).search(text="q", partitions=["p"], top_k=5, similarity_threshold=0.5)
    assert [c.id for c in out] == ["1"]  # related NOT included


@pytest.mark.asyncio
async def test_search_expands_related_when_requested():
    s = FakeSearcher()
    # The core expand helper only fetches related chunks for source
    # chunks that carry both a partition and a relationship_id.
    src = Chunk(id="1", text="t", partition="p", metadata={"_id": "1", "relationship_id": "r1"})
    s.search_result = [src]
    s.related_result = [_chunk("rel")]
    out = await _svc(s).search(
        text="q",
        partitions=["p"],
        top_k=5,
        similarity_threshold=0.5,
        include_related=True,
        related_limit=3,
    )
    ids = {c.id for c in out}
    assert "1" in ids and "rel" in ids


# --------------------------------------------------------------------------- #
# retrieve / retrieve_multi / fuse — powers QueryService (8C.2)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retrieve_single_query_via_pipeline():
    s = FakeSearcher()
    s.search_result = [_chunk("a"), _chunk("b")]
    out = await _svc(s).retrieve(partitions=["p"], query=Query(query="hi"))
    assert [c.id for c in out] == ["a", "b"]


@pytest.mark.asyncio
async def test_retrieve_multi_fuses_subqueries():
    s = FakeSearcher()
    s.search_result = [_chunk("a"), _chunk("b")]
    sq = SearchQueries(query_list=[Query(query="q1"), Query(query="q2")])
    out = await _svc(s).retrieve_multi(partitions=["p"], search_queries=sq)
    assert {c.id for c in out} == {"a", "b"}


@pytest.mark.asyncio
async def test_retrieve_per_query_returns_unfused_lists():
    s = FakeSearcher()
    s.search_result = [_chunk("a")]
    out = await _svc(s).retrieve_per_query(partitions=["p"], queries=[Query(query="q1"), Query(query="q2")])
    assert len(out) == 2
    assert all(lst[0].id == "a" for lst in out)


def test_fuse_rrf_merges_and_dedupes():
    a, b, c = _chunk("a"), _chunk("b"), _chunk("c")
    fused = RetrievalService.fuse([[a, b], [b, c]])
    ids = [x.id for x in fused]
    assert set(ids) == {"a", "b", "c"}
    assert ids[0] == "b"  # appears in both lists -> highest RRF score


def test_fuse_respects_top_k():
    a, b, c = _chunk("a"), _chunk("b"), _chunk("c")
    assert len(RetrievalService.fuse([[a, b], [b, c]], top_k=2)) == 2


# --------------------------------------------------------------------------- #
# Phase 14J.1 — per-partition retrieval pipeline config
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retrieve_uses_partition_retrieval_config_and_named_reranker():
    s = FakeSearcher()
    s.search_result = [_chunk("a"), _chunk("b"), _chunk("c")]
    reranker = FakeReranker()
    embedder_calls: list[str] = []
    reranker_calls: list[str] = []
    cfg = _config()
    cfg.partitions = {
        "tenant-a": _partition(
            retrieval=RetrievalPipelineConfig(
                top_k=3,
                top_n=2,
                similarity_threshold=0.77,
                include_related=False,
                include_ancestors=False,
                enable_reranker=True,
                reranker="fast-ranker",
            )
        )
    }

    svc = RetrievalService(
        searcher=s,
        reranker=None,
        llm=None,
        config=cfg,
        embedder_factory=lambda name: embedder_calls.append(name) or object(),
        reranker_factory=lambda name: reranker_calls.append(name) or reranker,
    )

    out = await svc.retrieve(partitions=["tenant-a"], query=Query(query="hello"))

    assert [c.id for c in out] == ["a", "b"]
    assert embedder_calls == ["embed-a"]
    assert reranker_calls == ["fast-ranker"]
    assert reranker.calls[0]["query"] == "hello"
    call = s.search_calls[0]
    assert call["partition"] == ["tenant-a"]
    assert call["top_k"] == 3
    assert call["similarity_threshold"] == 0.77


@pytest.mark.asyncio
async def test_retrieve_rejects_unknown_partition_when_partition_configs_exist():
    cfg = _config()
    cfg.partitions = {"tenant-a": _partition()}
    svc = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=cfg)

    with pytest.raises(PartitionNotFoundError, match="tenant-b"):
        await svc.retrieve(partitions=["tenant-b"], query=Query(query="hello"))
