from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig
from core.models.query import Query, SearchQueries
from core.retrieval.trace import canonical_fingerprint
from services.orchestrators.retrieval_service import RetrievalService


class _Searcher:
    async def search(self, **_kwargs):
        return []

    async def multi_query_search(self, **_kwargs):
        return []

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


def _partition(
    *,
    name: str = "tenant-a",
    chat_llm: str | None = None,
    retrieval: RetrievalPipelineConfig | None = None,
) -> PartitionConfig:
    return PartitionConfig(
        name=name,
        embedder="embed-a",
        chat_llm=chat_llm,
        indexation=IndexationPipelineConfig(),
        retrieval=retrieval or RetrievalPipelineConfig(),
    )


def test_public_configuration_uses_effective_contextualizer_and_expansion_llms():
    config = _config()
    config.models.llm = {
        "chat-model": SimpleNamespace(model_name="contextualizer-model"),
        "query-model": SimpleNamespace(model_name="query-expansion-model"),
    }
    config.partitions = {
        "tenant-a": _partition(
            chat_llm="chat-model",
            retrieval=RetrievalPipelineConfig(type="multiQuery", llm="query-model"),
        )
    }
    service = RetrievalService(
        searcher=_Searcher(),
        reranker=None,
        llm=None,
        config=config,
        llm_factory=lambda _name: object(),
    )

    public = service.public_retrieval_configuration(["tenant-a"])

    partition = public["partitions"][0]
    assert partition["contextualizer"]["model"] == "contextualizer-model"
    assert partition["retrieval"]["query_expansion_llm"]["model"] == "query-expansion-model"


@pytest.mark.parametrize(
    ("retrieval_type", "setting", "initial", "changed"),
    [
        ("single", "rrf_k", 42, 99),
        ("hyde", "hyde_prompt_name", "hyde-a", "hyde-b"),
        ("multiQuery", "multi_query_prompt_name", "multi-a", "multi-b"),
    ],
)
def test_configuration_fingerprint_tracks_pipeline_settings(retrieval_type, setting, initial, changed):
    config = _config()
    retrieval = RetrievalPipelineConfig(type=retrieval_type, **{setting: initial})
    config.partitions = {"tenant-a": _partition(retrieval=retrieval)}
    service = RetrievalService(searcher=_Searcher(), reranker=None, llm=None, config=config)

    first = service.configuration_fingerprint(["tenant-a"])
    setattr(retrieval, setting, changed)

    assert service.configuration_fingerprint(["tenant-a"]) != first


def test_configuration_fingerprint_expands_all_partitions():
    config = _config()
    tenant_a = RetrievalPipelineConfig(rrf_k=42)
    tenant_b = RetrievalPipelineConfig(rrf_k=60)
    config.partitions = {
        "tenant-b": _partition(name="tenant-b", retrieval=tenant_b),
        "tenant-a": _partition(name="tenant-a", retrieval=tenant_a),
    }
    service = RetrievalService(searcher=_Searcher(), reranker=None, llm=None, config=config)

    public = service.public_retrieval_configuration(["all"])
    first = service.configuration_fingerprint(["all"])
    tenant_b.rrf_k = 99

    assert [partition["name"] for partition in public["partitions"]] == ["tenant-a", "tenant-b"]
    assert service.configuration_fingerprint(["all"]) != first


@pytest.mark.asyncio
async def test_resolved_plan_reuses_the_prompt_used_for_execution_and_fingerprint():
    class _Prompts:
        def __init__(self):
            self.calls = 0

        async def resolve_prompt_with_identity(self, _prompt_type, names):
            self.calls += 1
            content = f"private prompt version {self.calls}"
            return SimpleNamespace(
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                name=names[0],
                source="named",
            )

    class _LLM:
        async def chat(self, _messages):
            return {"choices": [{"message": {"content": "generated query"}}]}

    config = _config()
    config.partitions = {
        "tenant-a": _partition(
            retrieval=RetrievalPipelineConfig(type="multiQuery", multi_query_prompt_name="legal-query")
        )
    }
    prompts = _Prompts()
    service = RetrievalService(
        searcher=_Searcher(),
        reranker=None,
        llm=None,
        config=config,
        llm_factory=lambda _name: _LLM(),
        prompt_service=prompts,
    )

    plan = await service.resolve_retrieval_plan(
        ["tenant-a"],
        contextualizer_prompt=SimpleNamespace(content_hash="context-hash", name=None, source="default"),
    )
    prompt = plan.public_configuration["partitions"][0]["retrieval"]["query_expansion_prompt"]

    assert prompts.calls == 1
    assert plan.groups[0].pipeline.retriever.multi_query_template == "private prompt version 1"
    assert prompt["content_hash"] == hashlib.sha256(b"private prompt version 1").hexdigest()
    assert plan.configuration_fingerprint == canonical_fingerprint(plan.public_configuration)

    await service.retrieve_multi(
        partitions=["tenant-a"],
        search_queries=SearchQueries(query_list=[Query(query="question")]),
        resolved_plan=plan,
    )
    assert prompts.calls == 1


@pytest.mark.asyncio
async def test_resolved_plan_records_effective_request_overrides():
    config = _config()
    config.partitions = {
        "tenant-a": _partition(
            retrieval=RetrievalPipelineConfig(
                top_k=60,
                similarity_threshold=0.6,
                enable_reranker=True,
                include_related=True,
                include_ancestors=True,
            )
        )
    }
    service = RetrievalService(searcher=_Searcher(), reranker=object(), llm=None, config=config)

    plan = await service.resolve_retrieval_plan(
        ["tenant-a"],
        top_k=25,
        similarity_threshold=0.3,
        disable_reranker=True,
        disable_expansion=True,
        contextualizer_prompt=SimpleNamespace(content_hash="hash", name=None, source="default"),
    )

    partition = plan.public_configuration["partitions"][0]
    assert partition["retrieval"]["top_k"] == 25
    assert partition["retrieval"]["similarity_threshold"] == 0.3
    assert partition["reranker"]["enabled"] is False
    assert partition["expansion"]["include_related"] is False
    assert partition["expansion"]["include_ancestors"] is False
