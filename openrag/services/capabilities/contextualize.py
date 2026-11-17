"""The ``contextualize`` step: the conversation rewritten into search queries.

With ``rag.query_rewrite`` (the default) the contextualizer prompt asks the
LLM for the sub-queries, their date filters and date order, the intent and
whether the turn needs retrieval at all; without it the latest user message is
searched as it is. The trace helpers record what was searched, and are also
used when contextualization is bypassed or never runs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from core.chat.history import latest_user_query
from core.models.query import Query, SearchQueries
from core.models.retrieval_trace import (
    ContextualizationTrace,
    ContextualizedSubqueryTrace,
    PromptTrace,
    TemporalFilterTrace,
    TraceError,
)
from core.prompts.query_contextualizer import build_contextualizer_messages, parse_search_queries
from core.retrieval.trace import RetrievalTraceBuilder
from core.utils.logging import get_logger
from services.orchestrators.prompt_service import ResolvedPrompt

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from services.orchestrators.prompt_service import PromptService

logger = get_logger()


class QueryContextualizer:
    """Rewrites a conversation into the queries retrieval searches.

    *llm* answers when the caller passes none. *clock* gives the current UTC
    time the date filters are resolved against; *detect_language* the
    language of the latest message.
    """

    def __init__(
        self,
        *,
        config: Settings,
        prompt_service: PromptService,
        llm: LLM,
        detect_language: Callable[[str], str | None],
        clock: Callable[[], datetime],
    ) -> None:
        # Live reference: the per-partition prompt names are read at request time.
        self._config = config
        self._prompt_service = prompt_service
        self._llm = llm
        self._detect_language = detect_language
        self._clock = clock
        self._query_rewrite = config.rag.query_rewrite
        self._max_contextualized_query_len = config.rag.max_contextualized_query_len

    @property
    def rewrites_queries(self) -> bool:
        """Whether the LLM rewrites the conversation (``rag.query_rewrite``) rather than passing it through."""
        return self._query_rewrite

    def _prompt_name(self, partitions: list[str] | None) -> str | None:
        """The contextualizer prompt the partition's retrieval preset names.

        Honoured only for a single owning partition, as the generation prompts
        are; multi-partition and the ``"all"`` sentinel resolve the global
        default.
        """
        if not partitions or "all" in partitions or len(partitions) != 1:
            return None
        cfg = self._config.partitions.get(partitions[0])
        if cfg is None:
            return None
        return getattr(getattr(cfg, "retrieval", None), "query_contextualizer_prompt_name", None)

    async def contextualize(
        self,
        messages: list[dict],
        *,
        llm: LLM | None = None,
        partitions: list[str] | None = None,
        trace: RetrievalTraceBuilder | None = None,
    ) -> SearchQueries:
        """The search queries for *messages*; the latest user message when the LLM fails twice."""
        llm = llm or self._llm
        last_user = latest_user_query(messages) or (messages[-1].get("content") or "")
        if not self._query_rewrite:
            queries = SearchQueries(query_list=[Query(query=last_user)])
            record_contextualization(trace, queries, original_query=last_user)
            return queries

        prompt_name = self._prompt_name(partitions)
        resolve_with_identity = getattr(self._prompt_service, "resolve_prompt_with_identity", None)
        if resolve_with_identity is not None:
            contextualizer = await resolve_with_identity("query_contextualizer", names=[prompt_name])
        else:
            content = await self._prompt_service.resolve_prompt("query_contextualizer", names=[prompt_name])
            contextualizer = ResolvedPrompt.create(
                content, name=prompt_name, source="named" if prompt_name else "default"
            )
        llm_messages = build_contextualizer_messages(
            contextualizer.content,
            messages=messages,
            query_language=self._detect_language(last_user),
            # UTC, not the host clock: see build_contextualizer_messages.
            now=self._clock(),
        )
        params = {
            "max_completion_tokens": self._max_contextualized_query_len,
            "response_format": {"type": "json_object"},
            # Structured extraction, not generation: sampling made the same
            # question flip between a date filter and none across requests.
            "temperature": 0,
        }
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                resp = await llm.chat(llm_messages, **params)
                content = resp["choices"][0]["message"]["content"]
                queries = parse_search_queries(content)
                record_contextualization(
                    trace,
                    queries,
                    original_query=last_user,
                    duration_seconds=time.perf_counter() - started,
                    llm=llm,
                    prompt=contextualizer,
                )
                return queries
            except Exception as exc:
                last_error = exc
                if attempt == 1:
                    logger.warning("Query generation parse error — retrying", error=str(exc))
                else:
                    logger.warning(
                        "Query generation failed twice — falling back to raw user query",
                        error=str(exc),
                    )
        queries = SearchQueries(query_list=[Query(query=last_user)])
        record_contextualization(
            trace,
            queries,
            original_query=last_user,
            duration_seconds=time.perf_counter() - started,
            llm=llm,
            prompt=contextualizer,
            fallback_used=True,
            error=last_error,
        )
        return queries


# --------------------------------------------------------------------------- #
# Trace
# --------------------------------------------------------------------------- #


def record_contextualization(
    trace: RetrievalTraceBuilder | None,
    queries: SearchQueries,
    *,
    original_query: str,
    duration_seconds: float = 0.0,
    llm: LLM | None = None,
    prompt: ResolvedPrompt | None = None,
    fallback_used: bool = False,
    error: Exception | None = None,
) -> None:
    """Record the queries searched for *original_query*."""
    if trace is None:
        return
    try:
        trace.contextualization = ContextualizationTrace(
            original_query=original_query,
            subqueries=[
                ContextualizedSubqueryTrace(
                    query=query.query,
                    temporal_filters=[
                        TemporalFilterTrace(operator=item.operator, value=item.value)
                        for item in (query.temporal_filters or [])
                    ],
                    date_order=query.date_order,
                )
                for query in queries.query_list
            ],
            intent=queries.intent,
            requires_retrieval=queries.requires_retrieval,
            fallback_used=fallback_used,
            error=(
                TraceError(stage="contextualization", message="redacted", kind=type(error).__name__)
                if error is not None
                else None
            ),
            duration_seconds=duration_seconds,
            model=getattr(llm, "_model", None),
            prompt=(
                PromptTrace(content_hash=prompt.content_hash, name=prompt.name, source=prompt.source)
                if prompt is not None
                else None
            ),
        )
        trace.timings["contextualization"] = duration_seconds
        trace.record_stage("original_query", status="complete", candidates=[])
        trace.record_stage("contextualized_query", status="complete", candidates=[])
    except Exception as trace_error:
        trace.record_error("contextualization", trace_error)


def record_bypass(trace: RetrievalTraceBuilder | None, *, original_query: str) -> None:
    """Record that the request asked to search its latest message as it is."""
    if trace is None:
        return
    try:
        trace.contextualization = ContextualizationTrace(
            original_query=original_query,
            bypassed=True,
        )
        trace.record_stage("original_query", status="complete", candidates=[])
        trace.record_stage("contextualized_query", status="not_run", candidates=[])
    except Exception as trace_error:
        trace.record_error("contextualization", trace_error)


def record_not_run(trace: RetrievalTraceBuilder | None, *, original_query: str) -> None:
    """Record that the request searched nothing, so nothing was contextualized."""
    if trace is None:
        return
    try:
        trace.contextualization = ContextualizationTrace(original_query=original_query)
        trace.record_stage("original_query", status="complete", candidates=[])
        trace.record_stage("contextualized_query", status="not_run", candidates=[])
    except Exception as trace_error:
        trace.record_error("contextualization", trace_error)


def mark_fallback(trace: RetrievalTraceBuilder | None, queries: SearchQueries) -> None:
    """Record that the contextualized queries were unusable and *queries* were searched instead."""
    if trace is None or trace.contextualization is None:
        return
    trace.contextualization = trace.contextualization.model_copy(
        update={
            "fallback_used": True,
            "subqueries": [
                ContextualizedSubqueryTrace(query=query.query, temporal_filters=[]) for query in queries.query_list
            ],
        }
    )
