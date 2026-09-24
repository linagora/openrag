from types import SimpleNamespace

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig
from core.retrieval.trace import RetrievalTraceBuilder
from services.orchestrators.retrieval_service import RetrievalService


class _Searcher:
    def __init__(self):
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
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
        embedder=SimpleNamespace(model_name="static-embedder"),
    )


def _partition(*, chat_llm=None, retrieval=None) -> PartitionConfig:
    return PartitionConfig(
        name="tenant-a",
        embedder="embed-a",
        chat_llm=chat_llm,
        indexation=IndexationPipelineConfig(),
        retrieval=retrieval or RetrievalPipelineConfig(),
    )


@pytest.mark.asyncio
async def test_search_forwards_request_local_trace():
    searcher = _Searcher()
    service = RetrievalService(searcher=searcher, reranker=None, llm=None, config=_config())
    trace = RetrievalTraceBuilder("request-1", "hello")

    await service.search(
        text="hello",
        partitions="tenant-a",
        top_k=7,
        similarity_threshold=0.8,
        trace=trace,
    )

    assert searcher.calls[0]["trace"] is trace


def test_raw_search_fingerprint_ignores_chat_pipeline_settings():
    config = _config()
    config.models.embedder = {"default": SimpleNamespace(model_name="embed-model", vector_field="vector")}
    retrieval = RetrievalPipelineConfig(type="single", rrf_k=42)
    config.partitions = {"tenant-a": _partition(chat_llm="chat-a", retrieval=retrieval)}
    service = RetrievalService(searcher=_Searcher(), reranker=None, llm=None, config=config)
    request_options = {"top_k": 25, "similarity_threshold": 0.4}

    first = service.search_configuration_fingerprint(["tenant-a"], request_options)
    retrieval.type = "multiQuery"
    retrieval.rrf_k = 99
    retrieval.query_contextualizer_prompt_name = "different-prompt"
    config.partitions["tenant-a"].chat_llm = "chat-b"

    assert service.search_configuration_fingerprint(["tenant-a"], request_options) == first


def test_raw_search_fingerprint_tracks_effective_request_options():
    config = _config()
    config.models.embedder = {"default": SimpleNamespace(model_name="embed-model", vector_field="vector")}
    service = RetrievalService(searcher=_Searcher(), reranker=None, llm=None, config=config)

    first = service.search_configuration_fingerprint(
        ["tenant-a"],
        {"top_k": 25, "similarity_threshold": 0.4},
    )
    second = service.search_configuration_fingerprint(
        ["tenant-a"],
        {"top_k": 50, "similarity_threshold": 0.4},
    )

    assert first != second


def test_raw_search_fingerprint_tracks_embedder_vector_field_and_hybrid_mode():
    config = _config()
    endpoint = SimpleNamespace(model_name="embed-model", vector_field="vector-a")
    config.models.embedder = {"default": endpoint}
    service = RetrievalService(searcher=_Searcher(), reranker=None, llm=None, config=config)
    options = {"top_k": 25, "similarity_threshold": 0.4}

    baseline = service.search_configuration_fingerprint(["tenant-a"], options)
    endpoint.vector_field = "vector-b"
    changed_field = service.search_configuration_fingerprint(["tenant-a"], options)
    endpoint.vector_field = "vector-a"
    config.vectordb.hybrid_search = False
    changed_hybrid = service.search_configuration_fingerprint(["tenant-a"], options)

    assert len({baseline, changed_field, changed_hybrid}) == 3
