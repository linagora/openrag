"""The ``retrieve`` step: the documents and the web results for a request's queries.

Documents come from ``RetrievalService.retrieve_multi``, which fuses the
sub-queries with each partition's ``rrf_k``; with web search on too, both are
searched in parallel. The comparison against the original query, a
diagnostic, also runs here: it retrieves again into a separate trace and
never changes the answer.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from core.chat.routing import Route
from core.models.query import Query, SearchQueries
from core.retrieval.trace import RetrievalDiagnosticsContext, RetrievalTraceBuilder

if TYPE_CHECKING:
    from core.chat.scope import Scope
    from core.models.chunk import Chunk
    from core.websearch import WebResult
    from services.capabilities.web_search import WebSearch
    from services.orchestrators.retrieval_service import RetrievalService


class SourceRetriever:
    """Searches the documents of a scope and, when the route says so, the web."""

    def __init__(self, *, retrieval_service: RetrievalService, web_search: WebSearch) -> None:
        self._retrieval = retrieval_service
        self._web = web_search

    async def retrieve(
        self,
        queries: SearchQueries,
        *,
        route: Route,
        scope: Scope,
        top_k: int | None,
        retrieval_top_k: Any,
        overrides: dict[str, Any],
        trace: RetrievalTraceBuilder | None = None,
        resolved_plan: Any = None,
    ) -> tuple[list[Chunk], list[WebResult]]:
        """The chunks and the web results, each empty when the route doesn't search it.

        *top_k* caps the chunks (map-reduce sets it), *retrieval_top_k* and
        *overrides* are the request's diagnostic retrieval settings, and
        *resolved_plan* the retrieval plan already resolved for the trace.
        """
        if not route.has_documents:
            return [], await self._web.search([q.query for q in queries.query_list])
        documents = self._documents(
            queries,
            scope=scope,
            top_k=top_k,
            retrieval_top_k=retrieval_top_k,
            overrides=overrides,
            trace=trace,
            resolved_plan=resolved_plan,
        )
        if not route.has_web:
            return await documents, []
        return tuple(await asyncio.gather(documents, self._web.search([q.query for q in queries.query_list])))

    def _documents(
        self,
        queries: SearchQueries,
        *,
        scope: Scope,
        top_k: int | None,
        retrieval_top_k: Any,
        overrides: dict[str, Any],
        trace: RetrievalTraceBuilder | None,
        resolved_plan: Any,
    ):
        return self._retrieval.retrieve_multi(
            partitions=scope.partitions,
            search_queries=queries,
            top_k=top_k,
            retrieval_top_k=retrieval_top_k,
            filter_params=scope.filter_params(traced=trace is not None),
            **overrides,
            **({"trace": trace} if trace is not None else {}),
            **({"resolved_plan": resolved_plan} if resolved_plan is not None else {}),
        )

    async def compare_original_query(
        self,
        trace: RetrievalTraceBuilder,
        *,
        original_query: str,
        scope: Scope,
        top_k: int | None,
        retrieval_top_k: Any,
        overrides: dict[str, Any],
        configuration_fingerprint: str,
        resolved_plan: Any = None,
    ) -> None:
        """Retrieve the original query into a separate trace, recorded under ``trace.comparisons``."""
        diagnostics = trace.diagnostics
        shadow = RetrievalTraceBuilder(
            request_id=f"{trace.request_id}:original",
            original_query=original_query,
            diagnostics=RetrievalDiagnosticsContext(
                candidate_limit=diagnostics.candidate_limit,
                query_mode=diagnostics.query_mode,
                compare_original_query=diagnostics.compare_original_query,
                effective_options=diagnostics.effective_options,
            ),
        )
        started = time.perf_counter()
        status = "complete"
        try:
            await self._documents(
                SearchQueries(query_list=[Query(query=original_query)]),
                scope=scope,
                top_k=top_k,
                retrieval_top_k=retrieval_top_k,
                overrides=overrides,
                trace=shadow,
                resolved_plan=resolved_plan,
            )
        except Exception as error:  # noqa: BLE001 - comparison is optional telemetry
            status = "error"
            shadow.record_error("original_query", error)
        shadow.timings["total"] = time.perf_counter() - started
        finished = shadow.finish(configuration_fingerprint=configuration_fingerprint)
        trace.comparisons["original_query"] = {
            "status": status,
            "stages": finished["stages"],
            "timings": finished["timings"],
            "errors": finished["errors"],
            "query_traces": finished.get("query_traces", []),
            "configuration_fingerprint": finished["configuration_fingerprint"],
            **{
                key: finished[key]
                for key in (
                    "candidate_limit",
                    "candidates_truncated",
                    "query_trace_limit",
                    "query_traces_truncated",
                    "trace_truncated",
                    "serialized_size_limit",
                )
                if key in finished
            },
        }
