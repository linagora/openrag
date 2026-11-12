from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig
from core.models.query import Query, SearchQueries
from core.retrieval.trace import canonical_fingerprint
from di.retrieval import create_retrieval_service


class _Searcher:
    async def search(self, **_kwargs):
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
            max_partition_concurrency=16,
        ),
        reranker=SimpleNamespace(enabled=False, top_k=5),
        partitions={},
        models=SimpleNamespace(embedder={}, reranker={}, llm={}),
        vectordb=SimpleNamespace(hybrid_search=True),
        rag=SimpleNamespace(mode="ChatBotRag", max_contextualized_query_len=512),
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


def test_public_configuration_uses_effective_contextualizer_llm():
    config = _config()
    config.models.llm = {"chat-model": SimpleNamespace(model_name="contextualizer-model")}
    # A preset stored for the removed multiQuery retriever: its type and LLM are ignored.
    config.partitions = {
        "tenant-a": _partition(
            chat_llm="chat-model",
            retrieval=RetrievalPipelineConfig(type="multiQuery", llm="query-model"),
        )
    }
    service = create_retrieval_service(
        searcher=_Searcher(),
        reranker=None,
        config=config,
        llm_factory=lambda _name: object(),
    )

    public = service.public_retrieval_configuration(["tenant-a"])

    partition = public["partitions"][0]
    assert partition["contextualizer"]["model"] == "contextualizer-model"
    assert partition["contextualizer"]["mode"] == "ChatBotRag"
    assert partition["contextualizer"]["max_contextualized_query_len"] == 512
    assert {"type", "query_expansion_llm", "query_expansion_prompt"}.isdisjoint(partition["retrieval"])


@pytest.mark.parametrize(
    ("setting", "changed"),
    [("mode", "SimpleRag"), ("max_contextualized_query_len", 1024)],
)
def test_configuration_fingerprint_tracks_rag_query_path_settings(setting, changed):
    config = _config()
    config.partitions = {"tenant-a": _partition()}
    service = create_retrieval_service(searcher=_Searcher(), reranker=None, config=config)

    first = service.configuration_fingerprint(["tenant-a"])
    setattr(config.rag, setting, changed)

    assert service.configuration_fingerprint(["tenant-a"]) != first


@pytest.mark.parametrize(
    ("setting", "initial", "changed"),
    [("rrf_k", 42, 99), ("top_k", 20, 40), ("similarity_threshold", 0.5, 0.7)],
)
def test_configuration_fingerprint_tracks_pipeline_settings(setting, initial, changed):
    config = _config()
    retrieval = RetrievalPipelineConfig(**{setting: initial})
    config.partitions = {"tenant-a": _partition(retrieval=retrieval)}
    service = create_retrieval_service(searcher=_Searcher(), reranker=None, config=config)

    first = service.configuration_fingerprint(["tenant-a"])
    setattr(retrieval, setting, changed)

    assert service.configuration_fingerprint(["tenant-a"]) != first


def test_public_configuration_reports_the_top_n_that_applies():
    config = _config()
    config.partitions = {
        "tenant-a": _partition(name="tenant-a"),
        "tenant-b": _partition(name="tenant-b", retrieval=RetrievalPipelineConfig(top_n=12)),
    }
    service = create_retrieval_service(searcher=_Searcher(), reranker=None, config=config)

    public = service.public_retrieval_configuration(["all"])

    assert [partition["reranker"]["top_n"] for partition in public["partitions"]] == [5, 12]


def test_configuration_fingerprint_tracks_global_top_n_when_preset_leaves_it_unset():
    config = _config()
    config.partitions = {"tenant-a": _partition()}
    service = create_retrieval_service(searcher=_Searcher(), reranker=None, config=config)

    first = service.configuration_fingerprint(["tenant-a"])
    config.reranker.top_k = 15

    assert service.configuration_fingerprint(["tenant-a"]) != first


def test_configuration_fingerprint_expands_all_partitions():
    config = _config()
    tenant_a = RetrievalPipelineConfig(rrf_k=42)
    tenant_b = RetrievalPipelineConfig(rrf_k=60)
    config.partitions = {
        "tenant-b": _partition(name="tenant-b", retrieval=tenant_b),
        "tenant-a": _partition(name="tenant-a", retrieval=tenant_a),
    }
    service = create_retrieval_service(searcher=_Searcher(), reranker=None, config=config)

    public = service.public_retrieval_configuration(["all"])
    first = service.configuration_fingerprint(["all"])
    tenant_b.rrf_k = 99

    assert [partition["name"] for partition in public["partitions"]] == ["tenant-a", "tenant-b"]
    assert service.configuration_fingerprint(["all"]) != first


@pytest.mark.asyncio
async def test_resolved_plan_is_executed_without_resolving_a_prompt():
    """Given the contextualizer prompt, neither the plan nor its execution resolves one."""

    class _Prompts:
        def __init__(self):
            self.calls = 0

        async def resolve_prompt_with_identity(self, _prompt_type, names, *, strict_errors=False):
            self.calls += 1
            raise AssertionError("no prompt should be resolved")

    config = _config()
    config.partitions = {"tenant-a": _partition()}
    prompts = _Prompts()
    service = create_retrieval_service(searcher=_Searcher(), reranker=None, config=config, prompt_service=prompts)

    plan = await service.resolve_retrieval_plan(
        ["tenant-a"],
        contextualizer_prompt=SimpleNamespace(content_hash="context-hash", name=None, source="default"),
    )
    assert plan.configuration_fingerprint == canonical_fingerprint(plan.public_configuration)

    await service.retrieve_multi(
        partitions=["tenant-a"],
        search_queries=SearchQueries(query_list=[Query(query="question")]),
        resolved_plan=plan,
    )
    assert prompts.calls == 0


@pytest.mark.asyncio
async def test_snapshot_plan_uses_strict_prompt_resolution_for_the_contextualizer_prompt():
    class _Prompts:
        def __init__(self):
            self.strict_values = []

        async def resolve_prompt_with_identity(self, prompt_type, names, *, strict_errors=False):
            self.strict_values.append((prompt_type, strict_errors))
            content = f"{prompt_type} prompt"
            return SimpleNamespace(
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                name=names[0] if names else None,
                source="named",
            )

    config = _config()
    config.partitions = {"tenant-a": _partition()}
    prompts = _Prompts()
    service = create_retrieval_service(
        searcher=_Searcher(),
        reranker=None,
        config=config,
        prompt_service=prompts,
    )

    await service.resolve_retrieval_plan(
        ["tenant-a"],
        build_execution=False,
        strict_prompt_resolution=True,
    )

    assert prompts.strict_values == [("query_contextualizer", True)]


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
    service = create_retrieval_service(searcher=_Searcher(), reranker=object(), config=config)

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


@pytest.mark.asyncio
async def test_resolved_snapshot_uses_global_reranker_for_legacy_preset():
    config = _config()
    config.reranker.enabled = True
    config.models.reranker = {"default": SimpleNamespace(model_name="legacy-reranker")}
    config.partitions = {
        "tenant-a": SimpleNamespace(
            embedder="embed-a",
            chat_llm=None,
            retrieval=SimpleNamespace(
                type="single",
                reranker="removed-reranker",
                top_k=20,
                similarity_threshold=0.5,
            ),
        )
    }

    def reranker_factory(name):
        if name == "default":
            return object()
        raise KeyError(name)

    service = create_retrieval_service(
        searcher=_Searcher(),
        reranker=object(),
        config=config,
        reranker_factory=reranker_factory,
    )

    plan = await service.resolve_retrieval_plan(
        ["tenant-a"],
        contextualizer_prompt=SimpleNamespace(content_hash="context-hash", name=None, source="default"),
        build_execution=False,
    )

    reranker = plan.public_configuration["partitions"][0]["reranker"]
    assert reranker["enabled"] is True
    assert reranker["name"] == "default"
    assert reranker["model"] == "legacy-reranker"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_factory", [False, True])
async def test_static_reranker_model_changes_resolved_fingerprint(has_factory):
    config = _config()
    config.reranker.model_name = "reranker-a"
    config.partitions = {"tenant-a": _partition(retrieval=RetrievalPipelineConfig(enable_reranker=True))}

    def missing_catalog_endpoint(name):
        raise KeyError(name)

    service = create_retrieval_service(
        searcher=_Searcher(),
        reranker=object(),
        config=config,
        reranker_factory=missing_catalog_endpoint if has_factory else None,
    )

    prompt = SimpleNamespace(content_hash="context-hash", name=None, source="default")
    first = await service.resolve_retrieval_plan(["tenant-a"], contextualizer_prompt=prompt, build_execution=False)
    config.reranker.model_name = "reranker-b"
    second = await service.resolve_retrieval_plan(["tenant-a"], contextualizer_prompt=prompt, build_execution=False)

    assert first.public_configuration["partitions"][0]["reranker"]["model"] == "reranker-a"
    assert second.public_configuration["partitions"][0]["reranker"]["model"] == "reranker-b"
    assert first.configuration_fingerprint != second.configuration_fingerprint


@pytest.mark.asyncio
async def test_resolved_plan_reloads_an_embedder_switched_by_another_process():
    config = _config()
    config.partitions = {"tenant-a": _partition()}

    class SwappedElsewhere:
        async def try_refresh_if_stale(self) -> None:
            config.partitions = {
                "tenant-a": PartitionConfig(
                    name="tenant-a",
                    embedder="embed-b",
                    indexation=IndexationPipelineConfig(),
                    retrieval=RetrievalPipelineConfig(),
                )
            }

    service = create_retrieval_service(
        searcher=_Searcher(), reranker=None, config=config, preset_service=SwappedElsewhere()
    )
    prompt = SimpleNamespace(content_hash="context-hash", name=None, source="default")
    plan = await service.resolve_retrieval_plan(["tenant-a"], contextualizer_prompt=prompt, build_execution=False)

    assert plan.public_configuration["partitions"][0]["embedder"]["name"] == "embed-b"
