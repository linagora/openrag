"""Unit tests for :class:`RetrievalService` (Phase 8C.1).

The Ray-backed searcher is faked (the service is constructed with a
``RetrievalSearcher`` stub, exactly as the container will inject
``MilvusRayShim``). Default config uses the ``single`` retriever with
the reranker disabled, so the core pipeline path is exercised end-to-end
without inference services.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.chunk import Chunk
from core.models.preset import PartitionConfig
from core.models.query import Query, SearchQueries
from core.retrieval.trace import RetrievalTraceBuilder, canonical_fingerprint
from core.utils.exceptions import PartitionNotFoundError
from services.orchestrators.prompt_service import ResolvedPrompt
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
            max_partition_concurrency=16,
        ),
        reranker=SimpleNamespace(enabled=reranker_enabled, top_k=5),
        partitions={},
        models=SimpleNamespace(reranker={}),
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
async def test_search_forwards_request_local_trace():
    searcher = FakeSearcher()
    trace = RetrievalTraceBuilder("req-1", "hello")

    await _svc(searcher).search(
        text="hello",
        partitions="p1",
        top_k=7,
        similarity_threshold=0.8,
        trace=trace,
    )

    assert searcher.search_calls[0]["trace"] is trace


def test_configuration_fingerprint_is_stable_and_ignores_secret_endpoint_fields():
    searcher = FakeSearcher()
    cfg = _config()
    cfg.vectordb = SimpleNamespace(hybrid_search=True, api_key="secret-a")
    cfg.embedder = SimpleNamespace(model_name="legacy-embedder", api_key="secret-b")
    cfg.partitions = {
        "tenant-b": _partition(name="tenant-b", embedder="embed-b"),
        "tenant-a": _partition(name="tenant-a", embedder="embed-a"),
    }
    cfg.models.embedder = {
        "embed-a": SimpleNamespace(
            model_name="model-a", endpoint="https://user:pass@example.test", extra={"api_key": "x"}
        ),
        "embed-b": SimpleNamespace(model_name="model-b", endpoint="https://example.test", extra={}),
    }
    service = RetrievalService(searcher=searcher, reranker=None, llm=None, config=cfg)

    first = service.configuration_fingerprint(["tenant-b", "tenant-a"])
    cfg.vectordb.api_key = "changed-secret"
    cfg.models.embedder["embed-a"].endpoint = "https://changed-secret.example.test"
    second = service.configuration_fingerprint(["tenant-a", "tenant-b"])

    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    ("retrieval_type", "setting", "initial", "changed"),
    [
        ("single", "rrf_k", 42, 99),
        ("hyde", "hyde_prompt_name", "hyde-a", "hyde-b"),
        ("multiQuery", "multi_query_prompt_name", "multi-a", "multi-b"),
    ],
)
def test_configuration_fingerprint_tracks_behavior_changing_pipeline_settings(
    retrieval_type,
    setting,
    initial,
    changed,
):
    config = _config()
    retrieval = RetrievalPipelineConfig(type=retrieval_type, **{setting: initial})
    config.partitions = {"tenant-a": _partition(retrieval=retrieval)}
    service = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=config)

    first = service.configuration_fingerprint(["tenant-a"])
    setattr(retrieval, setting, changed)
    second = service.configuration_fingerprint(["tenant-a"])

    assert first != second


@pytest.mark.parametrize(
    ("retrieval_type", "setting", "initial", "changed"),
    [
        ("multiQuery", "k_queries", 3, 5),
        ("hyde", "combine", False, True),
        ("single", "with_surrounding_chunks", False, True),
        ("single", "allow_filterless_fallback", True, False),
    ],
)
def test_configuration_fingerprint_tracks_effective_legacy_pipeline_settings(
    retrieval_type,
    setting,
    initial,
    changed,
):
    config = _config()
    setattr(config.retriever, setting, initial)
    config.partitions = {"tenant-a": _partition(retrieval=RetrievalPipelineConfig(type=retrieval_type))}
    service = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=config)

    first = service.configuration_fingerprint(["tenant-a"])
    setattr(config.retriever, setting, changed)
    second = service.configuration_fingerprint(["tenant-a"])

    assert first != second


def test_configuration_fingerprint_expands_all_to_configured_partitions():
    config = _config()
    tenant_a = RetrievalPipelineConfig(rrf_k=42)
    tenant_b = RetrievalPipelineConfig(rrf_k=60)
    config.partitions = {
        "tenant-b": _partition(name="tenant-b", retrieval=tenant_b),
        "tenant-a": _partition(name="tenant-a", retrieval=tenant_a),
    }
    service = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=config)

    public = service.public_retrieval_configuration(["all"])
    first = service.configuration_fingerprint(["all"])
    tenant_b.rrf_k = 99
    second = service.configuration_fingerprint(["all"])

    assert [partition["name"] for partition in public["partitions"]] == ["tenant-a", "tenant-b"]
    assert first != second


@pytest.mark.parametrize(
    ("retrieval_type", "prompt_type", "prompt_setting"),
    [
        ("hyde", "hyde", "hyde_prompt_name"),
        ("multiQuery", "multi_query", "multi_query_prompt_name"),
    ],
)
@pytest.mark.asyncio
async def test_resolved_configuration_fingerprint_tracks_query_expansion_prompt_hash(
    retrieval_type,
    prompt_type,
    prompt_setting,
):
    class PromptService:
        def __init__(self):
            self.content_hash = "expansion-hash-a"

        async def resolve_prompt_with_identity(self, resolved_type, names):
            assert resolved_type == prompt_type
            assert names == ["legal-expansion"]
            return SimpleNamespace(
                content="private query expansion instructions",
                content_hash=self.content_hash,
                name="legal-expansion",
                source="named",
            )

    config = _config()
    retrieval = RetrievalPipelineConfig(
        type=retrieval_type,
        **{prompt_setting: "legal-expansion"},
    )
    config.partitions = {"tenant-a": _partition(retrieval=retrieval)}
    prompt_service = PromptService()
    service = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=config,
        prompt_service=prompt_service,
    )
    contextualizer_prompt = SimpleNamespace(
        content_hash="contextualizer-hash",
        name="legal-contextualizer",
        source="named",
    )

    public = await service.resolved_public_retrieval_configuration(
        ["tenant-a"],
        contextualizer_prompt=contextualizer_prompt,
    )
    first = await service.resolved_configuration_fingerprint(
        ["tenant-a"],
        contextualizer_prompt=contextualizer_prompt,
    )
    prompt_service.content_hash = "expansion-hash-b"
    second = await service.resolved_configuration_fingerprint(
        ["tenant-a"],
        contextualizer_prompt=contextualizer_prompt,
    )

    assert public["partitions"][0]["retrieval"]["query_expansion_prompt"] == {
        "type": prompt_type,
        "name": "legal-expansion",
        "source": "named",
        "content_hash": "expansion-hash-a",
    }
    assert first != second
    assert "private query expansion instructions" not in json.dumps(public)


@pytest.mark.asyncio
async def test_query_expansion_prompt_legacy_resolver_does_not_infer_provenance():
    class LegacyPromptService:
        async def resolve_prompt(self, prompt_type, names):
            assert prompt_type == "hyde"
            assert names == ["missing-name"]
            return "resolved default prompt"

    config = _config()
    config.partitions = {
        "tenant-a": _partition(retrieval=RetrievalPipelineConfig(type="hyde", hyde_prompt_name="missing-name"))
    }
    service = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=config,
        prompt_service=LegacyPromptService(),
    )

    public = await service.resolved_public_retrieval_configuration(
        ["tenant-a"],
        contextualizer_prompt=SimpleNamespace(
            content_hash="contextualizer-hash",
            name="legal-contextualizer",
            source="named",
        ),
    )

    identity = public["partitions"][0]["retrieval"]["query_expansion_prompt"]
    assert identity["name"] is None
    assert identity["source"] is None
    assert identity["content_hash"] == hashlib.sha256(b"resolved default prompt").hexdigest()


@pytest.mark.asyncio
async def test_resolved_configuration_supports_slotted_resolved_prompt():
    prompt = ResolvedPrompt.create(
        "private prompt content",
        name="legal-contextualizer",
        source="named",
    )
    service = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=_config())

    public = await service.resolved_public_retrieval_configuration(
        ["tenant-a"],
        contextualizer_prompt=prompt,
    )

    assert public["contextualizer_prompt"] == {
        "name": "legal-contextualizer",
        "source": "named",
        "content_hash": hashlib.sha256(b"private prompt content").hexdigest(),
    }


@pytest.mark.asyncio
async def test_contextualizer_prompt_legacy_resolver_does_not_infer_provenance():
    class LegacyPromptService:
        async def resolve_prompt(self, prompt_type, names):
            assert prompt_type == "query_contextualizer"
            assert names == ["missing-name"]
            return "resolved default contextualizer"

    config = _config()
    config.partitions = {
        "tenant-a": _partition(retrieval=RetrievalPipelineConfig(query_contextualizer_prompt_name="missing-name"))
    }
    service = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=config,
        prompt_service=LegacyPromptService(),
    )

    public = await service.resolved_public_retrieval_configuration(["tenant-a"])

    assert public["contextualizer_prompt"] == {
        "name": None,
        "source": None,
        "content_hash": hashlib.sha256(b"resolved default contextualizer").hexdigest(),
    }


@pytest.mark.asyncio
async def test_resolved_configuration_fingerprint_tracks_prompt_hash_without_prompt_content():
    class PromptService:
        def __init__(self):
            self.content_hash = "prompt-hash-a"

        async def resolve_prompt_with_identity(self, prompt_type, names):
            assert prompt_type == "query_contextualizer"
            assert names == ["legal"]
            return SimpleNamespace(
                content="private contextualizer instructions",
                content_hash=self.content_hash,
                name="legal",
                source="named",
            )

    config = _config()
    config.partitions = {
        "tenant-a": _partition(retrieval=RetrievalPipelineConfig(query_contextualizer_prompt_name="legal"))
    }
    prompt_service = PromptService()
    service = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=config,
        prompt_service=prompt_service,
    )

    public = await service.resolved_public_retrieval_configuration(["tenant-a"])
    first = await service.resolved_configuration_fingerprint(["tenant-a"])
    prompt_service.content_hash = "prompt-hash-b"
    second = await service.resolved_configuration_fingerprint(["tenant-a"])

    assert public["contextualizer_prompt"] == {
        "name": "legal",
        "source": "named",
        "content_hash": "prompt-hash-a",
    }
    assert first == canonical_fingerprint(public)
    assert first != second
    assert "private contextualizer instructions" not in json.dumps(public)


@pytest.mark.asyncio
async def test_resolved_configuration_reuses_the_request_prompt_identity():
    class PromptService:
        async def resolve_prompt_with_identity(self, *_args, **_kwargs):
            raise AssertionError("the prompt used by the request must not be resolved again")

    service = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=_config(),
        prompt_service=PromptService(),
    )
    prompt = SimpleNamespace(
        content="private contextualizer instructions",
        content_hash="request-prompt-hash",
        name="legal",
        source="named",
    )

    public = await service.resolved_public_retrieval_configuration(
        ["tenant-a"],
        contextualizer_prompt=prompt,
    )

    assert public["contextualizer_prompt"] == {
        "name": "legal",
        "source": "named",
        "content_hash": "request-prompt-hash",
    }
    assert "private contextualizer instructions" not in json.dumps(public)


def test_public_configuration_preserves_expansion_settings():
    config = _config()
    config.vectordb = SimpleNamespace(hybrid_search=False)
    config.retriever.related_limit = 500
    config.retriever.max_ancestor_depth = None
    service = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=config)

    public = service.public_retrieval_configuration(["tenant-a"])

    assert public["partitions"][0]["expansion"]["related_limit"] == 500
    assert public["partitions"][0]["expansion"]["max_ancestor_depth"] is None


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
async def test_retrieve_threads_trace_through_pipeline_and_records_final_stage():
    s = FakeSearcher()
    s.search_result = [_chunk("a"), _chunk("b")]
    trace = RetrievalTraceBuilder("req-1", "hi")

    out = await _svc(s).retrieve(partitions=["p"], query=Query(query="hi"), trace=trace)

    assert [c.id for c in out] == ["a", "b"]
    assert s.search_calls[0]["trace"] is trace
    assert [c.id for c in trace.stages["final"].candidates] == ["a", "b"]


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


def test_fuse_trace_records_duplicates_scores_and_public_cutoff():
    first_b = _chunk("b")
    duplicate_b = _chunk("b")
    a, c = _chunk("a"), _chunk("c")
    trace = RetrievalTraceBuilder("req-1", "q")

    fused = RetrievalService.fuse([[a, first_b], [duplicate_b, c]], top_k=2, trace=trace)

    assert fused == [first_b, a]
    partition_fused = trace.stages["partition_fused"].candidates
    duplicate = next(candidate for candidate in partition_fused if candidate.duplicate_of is not None)
    assert duplicate.id == "b"
    assert duplicate.scores["fused"] == pytest.approx(1 / 62 + 1 / 61)
    assert next(candidate for candidate in partition_fused if candidate.id == "c").removal_reason.code == "final_top_n"
    assert [candidate.id for candidate in trace.stages["final"].candidates] == ["b", "a"]


@pytest.mark.asyncio
async def test_retrieve_multi_records_each_partition_group_trace():
    cfg = _config()
    cfg.partitions = {
        "a": _partition(name="a", embedder="embed-a"),
        "b": _partition(name="b", embedder="embed-b"),
    }
    svc = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=_mixed_factory(set()),
    )
    trace = RetrievalTraceBuilder("multi-partition", "hello")

    await svc.retrieve_multi(
        partitions=["a", "b"],
        search_queries=SearchQueries(query_list=[Query(query="rewritten one"), Query(query="rewritten two")]),
        trace=trace,
    )

    assert len(trace.query_traces) == 2
    assert [child.partition for child in trace.query_traces] == ["a", "b"]
    assert [child.query for child in trace.query_traces] == [None, None]
    assert [[nested.query for nested in child.query_traces] for child in trace.query_traces] == [
        ["rewritten one", "rewritten two"],
        ["rewritten one", "rewritten two"],
    ]
    assert all(
        next(stage for stage in query_trace.stages if stage.name == "pre_rerank").status == "complete"
        for partition_trace in trace.query_traces
        for query_trace in partition_trace.query_traces
    )
    assert trace.stages["pre_rerank"].status == "unavailable"


@pytest.mark.asyncio
async def test_retrieve_records_partition_identity_and_effective_query():
    cfg = _config()
    cfg.partitions = {
        "a": _partition(name="a", embedder="embed-a"),
        "b": _partition(name="b", embedder="embed-b"),
    }
    svc = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=_mixed_factory(set()),
    )
    trace = RetrievalTraceBuilder("multi-partition", "original follow-up")

    await svc.retrieve(
        partitions=["a", "b"],
        query=Query(query="contextualized question"),
        trace=trace,
    )

    assert [(child.partition, child.query) for child in trace.query_traces] == [
        ("a", "contextualized question"),
        ("b", "contextualized question"),
    ]


# --------------------------------------------------------------------------- #
# Phase 14J.1 — per-partition retrieval pipeline config
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retrieve_uses_partition_retrieval_config_and_named_reranker():
    s = FakeSearcher()
    s.search_result = [_chunk("a"), _chunk("b"), _chunk("c")]
    reranker = FakeReranker()
    searcher_calls: list[str] = []
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
        searcher_factory=lambda name: searcher_calls.append(name) or s,
        reranker_factory=lambda name: reranker_calls.append(name) or reranker,
    )

    out = await svc.retrieve(partitions=["tenant-a"], query=Query(query="hello"))

    assert [c.id for c in out] == ["a", "b"]
    assert searcher_calls == ["embed-a"]
    assert reranker_calls == ["fast-ranker"]
    assert reranker.calls[0]["query"] == "hello"
    call = s.search_calls[0]
    assert call["partition"] == ["tenant-a"]
    assert call["top_k"] == 3
    assert call["similarity_threshold"] == 0.77


@pytest.mark.asyncio
async def test_retrieve_diagnostic_overrides_threshold_depth_reranker_and_expansion():
    s = FakeSearcher()
    s.search_result = [_chunk("a"), _chunk("b")]
    reranker = FakeReranker()
    cfg = _config()
    cfg.partitions = {
        "tenant-a": _partition(
            retrieval=RetrievalPipelineConfig(
                top_k=3,
                top_n=2,
                similarity_threshold=0.77,
                include_related=True,
                include_ancestors=True,
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
        searcher_factory=lambda _name: s,
        reranker_factory=lambda _name: reranker,
    )

    await svc.retrieve_multi(
        partitions=["tenant-a"],
        search_queries=SearchQueries(query_list=[Query(query="hello")]),
        top_k=100,
        retrieval_top_k=100,
        similarity_threshold=0.35,
        disable_reranker=True,
        disable_expansion=True,
    )

    assert s.search_calls[0]["top_k"] == 100
    assert s.search_calls[0]["similarity_threshold"] == 0.35
    assert reranker.calls == []


@pytest.mark.asyncio
async def test_final_top_k_does_not_reduce_configured_search_depth():
    searcher = FakeSearcher()
    searcher.search_result = [_chunk("a"), _chunk("b"), _chunk("c")]
    cfg = _config()
    cfg.partitions = {"tenant-a": _partition(retrieval=RetrievalPipelineConfig(top_k=50, enable_reranker=False))}
    svc = RetrievalService(
        searcher=searcher,
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=lambda _name: searcher,
    )

    result = await svc.retrieve_multi(
        partitions=["tenant-a"],
        search_queries=SearchQueries(query_list=[Query(query="hello")]),
        top_k=2,
    )

    assert searcher.search_calls[0]["top_k"] == 50
    assert [chunk.id for chunk in result] == ["a", "b"]


@pytest.mark.asyncio
async def test_retrieve_falls_back_to_default_reranker_when_preset_stale():
    """A partition's ``reranker`` preset can go stale (renamed/deleted after
    assignment — this field has no create/PATCH-time validation, unlike
    ``chat_llm``). The stale name must fall back to the catalog default
    instead of raising."""
    s = FakeSearcher()
    s.search_result = [_chunk("a")]
    default_reranker = FakeReranker()
    reranker_calls: list[str] = []

    def factory(name: str):
        reranker_calls.append(name)
        if name == "default":
            return default_reranker
        raise KeyError(name)

    cfg = _config()
    cfg.partitions = {
        "tenant-a": _partition(retrieval=RetrievalPipelineConfig(enable_reranker=True, reranker="stale-ranker"))
    }

    svc = RetrievalService(
        searcher=s,
        reranker=None,
        llm=None,
        config=cfg,
        reranker_factory=factory,
    )

    out = await svc.retrieve(partitions=["tenant-a"], query=Query(query="hello"))

    assert [c.id for c in out] == ["a"]
    assert reranker_calls == ["stale-ranker", "default"]
    assert default_reranker.calls[0]["query"] == "hello"


@pytest.mark.asyncio
async def test_retrieve_falls_back_to_legacy_reranker_when_no_catalog_default():
    """No ``is_default`` reranker endpoint registered yet — fall back to the
    static reranker built at startup instead of raising."""
    s = FakeSearcher()
    s.search_result = [_chunk("a")]
    legacy_reranker = FakeReranker()

    cfg = _config()
    cfg.partitions = {"tenant-a": _partition(retrieval=RetrievalPipelineConfig(enable_reranker=True))}

    svc = RetrievalService(
        searcher=s,
        reranker=legacy_reranker,
        llm=None,
        config=cfg,
        reranker_factory=lambda name: (_ for _ in ()).throw(KeyError(name)),
    )

    out = await svc.retrieve(partitions=["tenant-a"], query=Query(query="hello"))

    assert [c.id for c in out] == ["a"]
    assert legacy_reranker.calls[0]["query"] == "hello"


@pytest.mark.asyncio
async def test_retrieve_uses_partition_searcher_factory_for_named_embedder():
    default_searcher = FakeSearcher()
    tenant_searcher = FakeSearcher()
    tenant_searcher.search_result = [_chunk("tenant")]
    searcher_names: list[str] = []
    cfg = _config()
    cfg.partitions = {"tenant-a": _partition(embedder="embed-a")}

    svc = RetrievalService(
        searcher=default_searcher,
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=lambda name: searcher_names.append(name) or tenant_searcher,
    )

    out = await svc.retrieve(partitions=["tenant-a"], query=Query(query="hello"))

    assert [c.id for c in out] == ["tenant"]
    assert searcher_names == ["embed-a"]
    assert default_searcher.search_calls == []
    assert tenant_searcher.search_calls[0]["partition"] == ["tenant-a"]


@pytest.mark.asyncio
async def test_retrieve_rejects_unknown_partition_when_partition_configs_exist():
    cfg = _config()
    cfg.partitions = {"tenant-a": _partition()}
    svc = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=cfg)

    with pytest.raises(PartitionNotFoundError, match="tenant-b"):
        await svc.retrieve(partitions=["tenant-b"], query=Query(query="hello"))


# --- #708: "all" must expand to per-partition pipelines (right embedder + top_n) ---


@pytest.mark.asyncio
async def test_retrieve_all_expands_to_per_partition_embedders():
    """Super-admin `openrag-all` reaches this layer as the literal ["all"].

    It previously collapsed to the default-embedder legacy pipeline and searched
    partition=["all"], so a partition indexed with a named embedder was queried
    with the wrong model. It must instead fan out to one pipeline per partition,
    each using that partition's embedder — the path named-partition search
    already uses.
    """
    per_embedder: dict[str, FakeSearcher] = {}

    def factory(name: str) -> FakeSearcher:
        s = per_embedder.setdefault(name, FakeSearcher())
        s.search_result = [_chunk(f"{name}-hit")]
        return s

    default_searcher = FakeSearcher()
    cfg = _config()
    cfg.partitions = {
        "p1": _partition(name="p1", embedder="embed-1"),
        "p2": _partition(name="p2", embedder="embed-2"),
    }
    svc = RetrievalService(
        searcher=default_searcher,
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=factory,
    )

    out = await svc.retrieve(partitions=["all"], query=Query(query="hello"))

    # Each partition searched with its OWN embedder, scoped to itself — never the
    # default searcher, never partition=["all"].
    assert set(per_embedder) == {"embed-1", "embed-2"}
    assert default_searcher.search_calls == []
    assert per_embedder["embed-1"].search_calls[0]["partition"] == ["p1"]
    assert per_embedder["embed-2"].search_calls[0]["partition"] == ["p2"]
    assert {c.id for c in out} == {"embed-1-hit", "embed-2-hit"}


@pytest.mark.asyncio
async def test_retrieve_all_applies_partition_top_n():
    """The reranker top_n was dropped on the `all` path (default_top_k was None).
    With expansion, each partition's top_n truncates its results."""
    s = FakeSearcher()
    s.search_result = [_chunk("a"), _chunk("b"), _chunk("c")]
    reranker = FakeReranker()
    cfg = _config()
    cfg.partitions = {
        "solo": _partition(
            name="solo",
            retrieval=RetrievalPipelineConfig(top_k=3, top_n=2, enable_reranker=True, reranker="r"),
        )
    }
    svc = RetrievalService(
        searcher=s,
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=lambda name: s,
        reranker_factory=lambda name: reranker,
    )

    out = await svc.retrieve(partitions=["all"], query=Query(query="hello"))

    assert [c.id for c in out] == ["a", "b"]  # truncated to top_n=2, not the full 3
    assert s.search_calls[0]["partition"] == ["solo"]


@pytest.mark.asyncio
async def test_retrieve_all_falls_back_to_legacy_when_no_partitions_exist():
    """On a fresh system with zero hydrated partitions there is nothing to
    expand — keep the single legacy pipeline searching ["all"]."""
    default_searcher = FakeSearcher()
    default_searcher.search_result = [_chunk("x")]
    cfg = _config()
    cfg.partitions = {}
    svc = RetrievalService(
        searcher=default_searcher,
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=lambda name: (_ for _ in ()).throw(AssertionError("factory must not be used")),
    )

    out = await svc.retrieve(partitions=["all"], query=Query(query="hello"))

    assert [c.id for c in out] == ["x"]
    assert default_searcher.search_calls[0]["partition"] == ["all"]


# --- #708: the "all" fan-out must be concurrency-bounded (production safety) ---


def _tracking_factory(state: dict):
    """searcher_factory whose searchers share a live-concurrency tracker."""

    def factory(_name: str) -> FakeSearcher:
        s = FakeSearcher()

        async def tracked_search(**kwargs):
            state["live"] += 1
            state["max"] = max(state["max"], state["live"])
            for _ in range(5):  # yield so queued searches get a chance to start
                await asyncio.sleep(0)
            state["live"] -= 1
            return [_chunk("hit")]

        s.search = tracked_search
        return s

    return factory


@pytest.mark.asyncio
async def test_retrieve_all_bounds_partition_fanout():
    """A large `all` fan-out must not launch one search per partition at once.
    With the cap at 2 over 6 partitions, no more than 2 run concurrently."""
    state = {"live": 0, "max": 0}
    cfg = _config()
    cfg.retriever.max_partition_concurrency = 2
    cfg.partitions = {f"p{i}": _partition(name=f"p{i}", embedder=f"e{i}") for i in range(6)}
    svc = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=_tracking_factory(state),
    )

    await svc.retrieve(partitions=["all"], query=Query(query="hi"))

    assert state["max"] <= 2, f"fan-out exceeded the cap: {state['max']}"
    assert state["max"] == 2, "the cap should still allow parallelism up to the limit"
    assert state["live"] == 0


@pytest.mark.asyncio
async def test_retrieve_small_fanout_stays_fully_parallel():
    """The fast path: a fan-out within the cap runs fully parallel — regular
    multi-partition users are not throttled and keep the prior behaviour."""
    state = {"live": 0, "max": 0}
    cfg = _config()
    cfg.retriever.max_partition_concurrency = 16
    cfg.partitions = {f"p{i}": _partition(name=f"p{i}", embedder=f"e{i}") for i in range(3)}
    svc = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=_tracking_factory(state),
    )

    await svc.retrieve(partitions=["all"], query=Query(query="hi"))

    assert state["max"] == 3, "all 3 partitions should run concurrently under the cap"


@pytest.mark.asyncio
async def test_pipeline_for_partition_threads_rrf_k():
    """A partition's rrf_k must reach its RetrieverPipeline (#707)."""
    cfg = _config()
    cfg.partitions = {"tenant-a": _partition(retrieval=RetrievalPipelineConfig(rrf_k=42))}
    svc = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=cfg)
    pipeline, _ = await svc._pipeline_for_partition("tenant-a")
    assert pipeline.rrf_k == 42


class _RecordingPromptService:
    def __init__(self, resolved: str):
        self._resolved = resolved
        self.calls: list[tuple[str, tuple]] = []

    async def resolve_prompt(self, prompt_type, names=None):
        self.calls.append((prompt_type, tuple(names or ())))
        return self._resolved


@pytest.mark.asyncio
async def test_hyde_template_resolved_from_preset_via_prompt_service():
    """A hyde preset's hyde_prompt_name is resolved through PromptService and
    threaded into the HyDeRetriever (the #13 retrieval-prompt seam)."""
    cfg = _config()
    cfg.partitions = {"tenant-a": _partition(retrieval=RetrievalPipelineConfig(type="hyde", hyde_prompt_name="myhyde"))}
    rec = _RecordingPromptService("HYDE {question}")
    svc = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=object(), config=cfg, prompt_service=rec)
    pipeline, _ = await svc._pipeline_for_partition("tenant-a")
    assert pipeline.retriever.hyde_template == "HYDE {question}"
    assert ("hyde", ("myhyde",)) in rec.calls


@pytest.mark.asyncio
async def test_multi_query_template_resolved_from_preset_via_prompt_service():
    cfg = _config()
    cfg.partitions = {
        "tenant-a": _partition(retrieval=RetrievalPipelineConfig(type="multiQuery", multi_query_prompt_name="mymq"))
    }
    rec = _RecordingPromptService("MQ {query} {k_queries}")
    svc = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=object(), config=cfg, prompt_service=rec)
    pipeline, _ = await svc._pipeline_for_partition("tenant-a")
    assert pipeline.retriever.multi_query_template == "MQ {query} {k_queries}"
    assert ("multi_query", ("mymq",)) in rec.calls


@pytest.mark.asyncio
async def test_single_strategy_resolves_no_prompt():
    """type=single needs no expansion prompt — PromptService is never called."""
    cfg = _config()
    cfg.partitions = {"tenant-a": _partition(retrieval=RetrievalPipelineConfig(type="single"))}
    rec = _RecordingPromptService("unused")
    svc = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=cfg, prompt_service=rec)
    await svc._pipeline_for_partition("tenant-a")
    assert rec.calls == []


# --------------------------------------------------------------------------- #
# Partition fan-out resilience (#736)
# --------------------------------------------------------------------------- #


class ExplodingSearcher(FakeSearcher):
    """A searcher whose partition is unreachable — e.g. its embedder endpoint is
    down, which is the realistic per-leg failure now that every partition shares
    one Milvus collection."""

    async def search(self, **kwargs):
        raise RuntimeError("embedder endpoint unreachable")


def _mixed_factory(failing: set[str]):
    """Searcher factory where the named embedders raise and the rest answer."""
    made: dict[str, FakeSearcher] = {}

    def factory(name: str) -> FakeSearcher:
        if name not in made:
            s = ExplodingSearcher() if name in failing else FakeSearcher()
            s.search_result = [_chunk(f"{name}-hit")]
            made[name] = s
        return made[name]

    return factory


@pytest.mark.asyncio
async def test_retrieve_survives_one_failing_partition():
    """One unhealthy partition must not wipe out the healthy ones (#736).

    The fan-out gathers one leg per partition group. Without
    ``return_exceptions`` a single raising leg aborts the whole gather, so a
    user with several memberships — or a super-admin on ``all`` — gets nothing
    back instead of the partitions that answered fine.
    """
    cfg = _config()
    cfg.partitions = {
        "good": _partition(name="good", embedder="embed-good"),
        "bad": _partition(name="bad", embedder="embed-bad"),
    }
    svc = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=_mixed_factory({"embed-bad"}),
    )
    trace = RetrievalTraceBuilder("degraded-single", "hello")

    out = await svc.retrieve(partitions=["all"], query=Query(query="hello"), trace=trace)

    assert [c.id for c in out] == ["embed-good-hit"]
    assert [candidate.id for candidate in trace.stages["final"].candidates] == ["embed-good-hit"]


@pytest.mark.asyncio
async def test_retrieve_multi_survives_one_failing_partition():
    """The multi-query fan-out shares the same choke point, so it degrades too."""
    cfg = _config()
    cfg.partitions = {
        "good": _partition(name="good", embedder="embed-good"),
        "bad": _partition(name="bad", embedder="embed-bad"),
    }
    svc = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=_mixed_factory({"embed-bad"}),
    )
    trace = RetrievalTraceBuilder("degraded-multi", "hello")

    out = await svc.retrieve_multi(
        partitions=["all"],
        search_queries=SearchQueries(query_list=[Query(query="hello")]),
        trace=trace,
    )

    assert [c.id for c in out] == ["embed-good-hit"]
    assert [candidate.id for candidate in trace.stages["final"].candidates] == ["embed-good-hit"]


@pytest.mark.asyncio
async def test_retrieve_raises_when_every_partition_fails():
    """Fail-open stops at total failure.

    With no leg left there is nothing to degrade to, and an empty list reads as
    "the corpus has no match" — the caller would answer from no context instead
    of surfacing the outage. The original error type is preserved so the API
    keeps mapping it as before.
    """
    cfg = _config()
    cfg.partitions = {
        "a": _partition(name="a", embedder="embed-a"),
        "b": _partition(name="b", embedder="embed-b"),
    }
    svc = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=_mixed_factory({"embed-a", "embed-b"}),
    )

    with pytest.raises(RuntimeError, match="embedder endpoint unreachable"):
        await svc.retrieve(partitions=["all"], query=Query(query="hello"))


@pytest.mark.asyncio
async def test_retrieve_propagates_cancellation_rather_than_degrading():
    """A cancelled request is not a degraded partition.

    ``return_exceptions=True`` captures ``CancelledError`` like any other
    exception, which would silently turn a client disconnect or a timeout into
    a partial result. It must unwind instead.
    """

    class CancellingSearcher(FakeSearcher):
        async def search(self, **kwargs):
            raise asyncio.CancelledError()

    def factory(name: str) -> FakeSearcher:
        if name == "embed-cancel":
            return CancellingSearcher()
        s = FakeSearcher()
        s.search_result = [_chunk(f"{name}-hit")]
        return s

    cfg = _config()
    cfg.partitions = {
        "good": _partition(name="good", embedder="embed-good"),
        "gone": _partition(name="gone", embedder="embed-cancel"),
    }
    svc = RetrievalService(searcher=FakeSearcher(), reranker=None, llm=None, config=cfg, searcher_factory=factory)

    with pytest.raises(asyncio.CancelledError):
        await svc.retrieve(partitions=["all"], query=Query(query="hello"))


@pytest.mark.asyncio
async def test_bounded_fanout_also_degrades_per_partition():
    """Resilience must hold on the throttled path too, not just the fast one.

    Above ``max_partition_concurrency`` the legs run through a semaphore
    wrapper, which is a separate gather call — the earlier fix would have been
    easy to apply to only one of them.
    """
    cfg = _config()
    cfg.retriever.max_partition_concurrency = 2
    cfg.partitions = {f"p{i}": _partition(name=f"p{i}", embedder=f"e{i}") for i in range(5)}
    svc = RetrievalService(
        searcher=FakeSearcher(),
        reranker=None,
        llm=None,
        config=cfg,
        searcher_factory=_mixed_factory({"e1", "e3"}),
    )

    out = await svc.retrieve(partitions=["all"], query=Query(query="hi"))

    assert sorted(c.id for c in out) == ["e0-hit", "e2-hit", "e4-hit"]


@pytest.mark.asyncio
async def test_dropped_partition_is_named_in_the_log():
    """Degrading silently would hide a broken partition indefinitely: results
    still come back, so nobody notices until someone asks why a tenant's
    documents stopped being cited. The warning names the partitions (#736)."""
    from loguru import logger as _logger

    messages: list[str] = []
    sink_id = _logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        cfg = _config()
        cfg.partitions = {
            "healthy": _partition(name="healthy", embedder="embed-good"),
            "broken": _partition(name="broken", embedder="embed-bad"),
        }
        svc = RetrievalService(
            searcher=FakeSearcher(),
            reranker=None,
            llm=None,
            config=cfg,
            searcher_factory=_mixed_factory({"embed-bad"}),
        )
        await svc.retrieve(partitions=["all"], query=Query(query="hello"))
    finally:
        _logger.remove(sink_id)

    dropped = [m for m in messages if "broken" in m]
    assert dropped, f"the dropped partition was not logged: {messages}"
    assert "RuntimeError" in dropped[0]
    assert "healthy" not in dropped[0], "only the failed partition should be named"
