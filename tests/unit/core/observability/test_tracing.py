"""Request tracing: off by default, and the shape of a traced retrieval when on."""

from __future__ import annotations

import json
import uuid

import pytest
from core.models.chunk import Chunk
from core.models.query import Query, SearchQueries, TemporalPredicate
from core.observability import tracing
from core.retrieval.pipeline import RetrieverPipeline
from core.retrieval.retriever import Retriever
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class _Retriever(Retriever):
    def __init__(self, *results: list[Chunk]) -> None:
        self.results = list(results)

    async def retrieve(self, partition, query, filter=None, filter_params=None):
        return self.results.pop(0) if self.results else []

    async def expand_search_results(self, results, filter_params=None):
        return results


class _ReverseReranker:
    async def rerank(self, query, documents, top_k=None):
        return [(i, float(i)) for i in range(len(documents) - 1, -1, -1)]


def _chunks(*ids: str) -> list[Chunk]:
    return [Chunk(id=i, document_id=f"file-{i}", text=f"text of {i}", partition="p1") for i in ids]


@pytest.fixture
def no_langfuse(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    tracing._client.cache_clear()
    yield
    tracing._client.cache_clear()


@pytest.fixture
def spans(monkeypatch):
    """A Langfuse client whose spans also land in memory, as if Langfuse were configured."""
    from langfuse import Langfuse

    # The SDK keeps one set of resources per public key: a fresh key gets this provider.
    public_key = f"pk-lf-{uuid.uuid4().hex}"
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", public_key)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = Langfuse(
        public_key=public_key,
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        tracer_provider=provider,
        flush_interval=3600,
    )
    tracing._client.cache_clear()
    monkeypatch.setattr(tracing, "_client", lambda: client)
    yield exporter
    provider.shutdown()


def _tree(exporter: InMemorySpanExporter) -> dict[str, str | None]:
    """Each span name mapped to its parent's name."""
    finished = exporter.get_finished_spans()
    names = {span.context.span_id: span.name for span in finished}
    return {span.name: names.get(span.parent.span_id) if span.parent else None for span in finished}


@pytest.mark.asyncio
async def test_without_keys_nothing_is_recorded_and_the_sdk_is_not_started(no_langfuse):
    with tracing.start_trace("chat-completion", input="hi") as root:
        assert root is tracing.NOOP
        assert not tracing.recording()
        with tracing.observe("retrieve-documents") as step:
            assert step is tracing.NOOP
    assert tracing.new_trace_id() is None
    assert not tracing.enabled()


@pytest.mark.asyncio
async def test_steps_outside_a_trace_record_nothing(spans):
    # e.g. the LLM client called from an indexing actor
    with tracing.observe("llm-chat", as_type="generation") as generation:
        assert generation is tracing.NOOP
    assert tracing.start("llm-chat") is tracing.NOOP
    assert spans.get_finished_spans() == ()


@pytest.mark.asyncio
async def test_a_traced_retrieval_nests_each_stage_under_its_subquery(spans):
    dated = Query(query="q1", temporal_filters=[TemporalPredicate(operator=">=", value="2026-01-01T00:00:00+00:00")])
    pipeline = RetrieverPipeline(
        # q1's dated search finds nothing, so it falls back to an unfiltered one.
        retriever=_Retriever([], _chunks("a", "b"), _chunks("b", "c")),
        reranker=_ReverseReranker(),
    )
    with tracing.start_trace("chat-completion", input="hi", trace_id=tracing.new_trace_id(seed="req-1")):
        fused = await pipeline.get_relevant_docs(
            partition=["p1"], search_queries=SearchQueries(query_list=[dated, Query(query="q2")])
        )

    tree = _tree(spans)
    assert tree["chat-completion"] is None
    assert tree["retrieve-subquery"] == "chat-completion"
    assert tree["rerank-candidates"] == "retrieve-subquery"
    assert tree["drop-temporal-filter"] == "retrieve-subquery"
    assert tree["fuse-subqueries"] == "chat-completion"
    assert {c.id for c in fused} == {"a", "b", "c"}

    by_name = {span.name: span for span in spans.get_finished_spans()}
    assert {span.context.trace_id for span in spans.get_finished_spans()} == {
        int(tracing.new_trace_id(seed="req-1"), 16)
    }
    rerank_output = json.loads(by_name["rerank-candidates"].attributes["langfuse.observation.output"])
    assert [entry["chunk_id"] for entry in rerank_output] in (["b", "a"], ["c", "b"])
    assert all("rerank_score" in entry and entry["text"].startswith("text of") for entry in rerank_output)


def test_describe_chunks_keeps_rank_scores_and_a_bounded_preview():
    chunk = Chunk(id="a", document_id="f", text="x" * 1000, partition="p1", metadata={"filename": "doc.pdf"})
    (entry,) = tracing.describe_chunks([chunk], scores=[0.42])
    assert entry == {
        "rank": 1,
        "chunk_id": "a",
        "file_id": "f",
        "partition": "p1",
        "filename": "doc.pdf",
        "score": 0.42,
        "text": "x" * tracing.TEXT_PREVIEW_CHARS,
    }


def test_pipeline_fingerprint_changes_with_its_settings():
    base = RetrieverPipeline(retriever=_Retriever(), rrf_k=60)
    tuned = RetrieverPipeline(retriever=_Retriever(), rrf_k=10)
    assert tracing.fingerprint(base.describe()) == tracing.fingerprint(base.describe())
    assert tracing.fingerprint(base.describe()) != tracing.fingerprint(tuned.describe())
