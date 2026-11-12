"""The retrieve step: documents and web results per route, and the original-query comparison."""

from __future__ import annotations

import pytest
from core.chat.routing import Route
from core.chat.scope import Scope
from core.models.chunk import Chunk
from core.models.query import Query, SearchQueries
from core.models.retrieval_trace import TraceCandidate
from core.retrieval.trace import TRACE_FILE_SCOPE_KIND_KEY, RetrievalDiagnosticsContext, RetrievalTraceBuilder
from core.websearch import WebResult
from services.capabilities.retrieve import SourceRetriever
from services.capabilities.web_search import WebSearch


class FakeRetrieval:
    def __init__(self, chunks=None):
        self._chunks = chunks if chunks is not None else [Chunk(id="c1", text="ctx", metadata={"_id": "c1"})]
        self.retrieve_multi_calls: list[dict] = []

    async def retrieve_multi(self, **kwargs):
        self.retrieve_multi_calls.append(kwargs)
        return list(self._chunks)


class FakeWeb:
    max_tokens = 2000

    def __init__(self):
        self.calls: list[str] = []

    async def search(self, query):
        self.calls.append(query)
        return [WebResult(title=query, url=f"https://example.test/{query}", snippet="")]


def _retriever(retrieval=None, web=None) -> SourceRetriever:
    return SourceRetriever(retrieval_service=retrieval or FakeRetrieval(), web_search=WebSearch(web or FakeWeb()))


QUERIES = SearchQueries(query_list=[Query(query="notice"), Query(query="probation")])


async def _retrieve(retriever, route, scope=Scope(["p"]), trace=None):
    return await retriever.retrieve(
        QUERIES, route=route, scope=scope, top_k=None, retrieval_top_k=None, overrides={}, trace=trace
    )


@pytest.mark.asyncio
async def test_documents_only_never_searches_the_web():
    retrieval, web = FakeRetrieval(), FakeWeb()
    chunks, web_results = await _retrieve(_retriever(retrieval, web), Route.DOCUMENTS)
    assert [c.id for c in chunks] == ["c1"]
    assert web_results == []
    assert web.calls == []
    assert retrieval.retrieve_multi_calls[0]["search_queries"] is QUERIES


@pytest.mark.asyncio
async def test_web_only_never_searches_the_documents():
    retrieval, web = FakeRetrieval(), FakeWeb()
    chunks, web_results = await _retrieve(_retriever(retrieval, web), Route.WEB, scope=Scope(None))
    assert chunks == []
    assert [r.title for r in web_results] == ["notice", "probation"]
    assert retrieval.retrieve_multi_calls == []


@pytest.mark.asyncio
async def test_documents_and_web_search_both():
    retrieval, web = FakeRetrieval(), FakeWeb()
    chunks, web_results = await _retrieve(_retriever(retrieval, web), Route.DOCUMENTS_AND_WEB)
    assert [c.id for c in chunks] == ["c1"]
    assert web.calls == ["notice", "probation"]
    assert len(retrieval.retrieve_multi_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("traced", [False, True])
async def test_the_scope_filters_the_documents(traced):
    retrieval = FakeRetrieval()
    trace = RetrievalTraceBuilder("request", "notice") if traced else None
    scope = Scope(["legal"], ["contract"], "attachment", ["contract"])

    await _retrieve(_retriever(retrieval), Route.DOCUMENTS, scope=scope, trace=trace)

    call = retrieval.retrieve_multi_calls[0]
    assert call["partitions"] == ["legal"]
    expected = {"file_id": ["contract"], **({TRACE_FILE_SCOPE_KIND_KEY: "attachment"} if traced else {})}
    assert call["filter_params"] == expected
    assert ("trace" in call) is traced


@pytest.mark.asyncio
async def test_original_query_comparison_preserves_partition_child_traces():
    class PartitionedRetrieval(FakeRetrieval):
        async def retrieve_multi(self, **kwargs):
            trace = kwargs["trace"]
            child = RetrievalTraceBuilder(
                "partition-child",
                "original query",
                partition="legal",
                diagnostics=trace.diagnostics,
            )
            child.record_stage("final", status="complete", candidates=[])
            trace.record_query_trace(child)
            return await super().retrieve_multi(**kwargs)

    retrieval = PartitionedRetrieval()
    service = _retriever(retrieval)
    trace = RetrievalTraceBuilder("request", "original query")

    await service.compare_original_query(
        trace,
        original_query="original query",
        scope=Scope(["legal"]),
        top_k=10,
        retrieval_top_k=10,
        overrides={},
        configuration_fingerprint="fingerprint",
    )

    comparison = trace.finish(configuration_fingerprint="fingerprint")["comparisons"]["original_query"]
    assert comparison["query_traces"][0]["partition"] == "legal"
    assert comparison["query_traces"][0]["query"] == "original query"


@pytest.mark.asyncio
async def test_original_query_comparison_has_an_independent_candidate_budget():
    class CandidateHeavyRetrieval(FakeRetrieval):
        async def retrieve_multi(self, **kwargs):
            trace = kwargs["trace"]
            for stage in ("original_query", "contextualized_query", "dense_before_threshold", "final"):
                candidates = [TraceCandidate(id=f"{stage}-{index}", rank=index + 1) for index in range(150)]
                trace.record_stage(stage, status="complete", candidates=candidates)
            return await super().retrieve_multi(**kwargs)

    diagnostics = RetrievalDiagnosticsContext(candidate_limit=500)
    trace = RetrievalTraceBuilder("request", "original query", diagnostics=diagnostics)
    for stage in ("original_query", "contextualized_query", "dense_before_threshold"):
        candidates = [TraceCandidate(id=f"main-{stage}-{index}", rank=index + 1) for index in range(150)]
        trace.record_stage(stage, status="complete", candidates=candidates)

    service = _retriever(CandidateHeavyRetrieval())
    await service.compare_original_query(
        trace,
        original_query="original query",
        scope=Scope(["legal"]),
        top_k=None,
        retrieval_top_k=50,
        overrides={},
        configuration_fingerprint="fingerprint",
    )

    comparison = trace.finish(configuration_fingerprint="fingerprint")["comparisons"]["original_query"]
    assert comparison["candidate_limit"] == 500
    assert comparison["candidates_truncated"] is True
    assert diagnostics.remaining_candidates == 50
    assert diagnostics.candidates_truncated is False
    assert "candidates_truncated" not in trace.finish(configuration_fingerprint="fingerprint")


@pytest.mark.asyncio
async def test_original_query_comparison_reports_truncated_partition_traces():
    class FanoutRetrieval(FakeRetrieval):
        async def retrieve_multi(self, **kwargs):
            trace = kwargs["trace"]
            for index in range(501):
                trace.record_query_trace(
                    RetrievalTraceBuilder(
                        f"partition-{index}",
                        "original query",
                        partition=f"partition-{index}",
                        diagnostics=trace.diagnostics,
                    )
                )
            return await super().retrieve_multi(**kwargs)

    service = _retriever(FanoutRetrieval())
    trace = RetrievalTraceBuilder("request", "original query")

    await service.compare_original_query(
        trace,
        original_query="original query",
        scope=Scope(["all"]),
        top_k=10,
        retrieval_top_k=10,
        overrides={},
        configuration_fingerprint="fingerprint",
    )

    comparison = trace.finish(configuration_fingerprint="fingerprint")["comparisons"]["original_query"]
    assert len(comparison["query_traces"]) == 500
    assert comparison["query_traces_truncated"] is True
    assert comparison["trace_truncated"] is True
