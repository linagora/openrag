"""Retrieval pipeline: per-query retrieval, optional temporal-filter fallback,
optional reranking, optional related/ancestor expansion, and RRF fusion across
sub-queries.

Extracted from ``components/pipeline.py:RetrieverPipeline``. The legacy
``RagPipeline`` (LLM-driven query generation, system-prompt assembly,
streaming) lives in the orchestrator layer and is rebuilt in Phase 8.

This pipeline depends only on core ABCs:

  * ``Retriever``       — strategy that produces candidate chunks
  * ``Reranker``        — optional cross-encoder reranker (per Phase 4 ABC:
                          returns ``[(idx, score), ...]`` over a list of texts)
  * ``RetrievalSearcher`` is consumed by the retriever, not directly here.

Config knobs are constructor arguments; there is no module-level config load.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

from core.models.chunk import Chunk
from core.models.query import Query, SearchQueries
from core.models.retrieval_result import ScoredChunk
from core.models.retrieval_trace import TraceCandidate, TraceRemovalReason
from core.rerankers.reranker import Reranker
from core.retrieval.retriever import Retriever
from core.retrieval.rrf import rrf_reranking
from core.retrieval.trace import RetrievalTraceBuilder, merge_child_traces


def _chunk_key(c: Chunk) -> Any:
    """Identity key for fusion / dedup. Falls back to object id when missing."""
    return c.id or id(c)


def _safe_trace(trace: RetrievalTraceBuilder | None, stage: str, operation) -> None:
    if trace is None:
        return
    try:
        operation()
    except Exception as error:
        try:
            trace.record_error(stage, error)
        except Exception:
            pass


def _with_removal_reasons(
    candidates: list[TraceCandidate],
    removed_ids: set[str],
    code: str,
    explanation: str,
) -> list[TraceCandidate]:
    return [
        candidate.model_copy(update={"removal_reason": TraceRemovalReason(code=code, explanation=explanation)})
        if candidate.id in removed_ids and candidate.duplicate_of is None
        else candidate
        for candidate in candidates
    ]


async def _rerank_chunks(reranker: Reranker, query: str, chunks: list[Chunk]) -> list[Chunk]:
    """Reorder chunks via the Reranker ABC.

    The ABC scores text+query pairs and returns ``[(orig_index, score), ...]``;
    we look up the original chunk for each ranked index. Items the reranker
    drops are excluded.

    Each survivor comes back as a :class:`ScoredChunk` carrying its score.
    ``ScoredChunk`` is a ``Chunk`` subclass, so this stays a ``list[Chunk]`` for
    every caller downstream while the score travels as a typed field rather than
    a magic metadata key. It reaches clients via ``ScoredChunk.to_langchain()``,
    which folds the scores into the metadata that API source entries are built
    from. A chunk that never met a reranker stays a plain ``Chunk`` and simply
    has no score — not a null, and not a 0.0 that reads like a real one.
    """
    if not chunks:
        return chunks
    ranking = await reranker.rerank(query=query, documents=[c.text for c in chunks], top_k=None)
    return [ScoredChunk.from_chunk(chunks[idx], rerank_score=score) for idx, score in ranking]


class RetrieverPipeline:
    """Orchestrates retrieval + reranking + expansion for a list of sub-queries.

    Args:
        retriever: Concrete retrieval strategy (Single / MultiQuery / HyDe).
        reranker: Reranker implementation, or ``None`` to skip reranking.
        reranker_top_k: When expansion is enabled, the top-K size used to
                        decide which results to expand.
        allow_filterless_fallback: If a temporal filter wipes out all
                        candidates, retry once without it. When ``False``,
                        return zero docs rather than ones outside the
                        temporal range.
    """

    def __init__(
        self,
        retriever: Retriever,
        reranker: Reranker | None = None,
        reranker_top_k: int = 5,
        allow_filterless_fallback: bool = True,
        rrf_k: int = 60,
    ) -> None:
        self.retriever = retriever
        self.reranker = reranker
        self.reranker_top_k = reranker_top_k
        self.allow_filterless_fallback = allow_filterless_fallback
        # RRF dampening for fusing this partition's multiQuery sub-query rankings
        # (see get_relevant_docs). 60 is canonical; a preset can tune it via
        # RetrievalPipelineConfig.rrf_k. Cross-partition fusion happens a layer
        # up in RetrievalService.fuse, which is not partition-scoped.
        self.rrf_k = rrf_k

    @property
    def reranker_enabled(self) -> bool:
        return self.reranker is not None

    @property
    def expansion_enabled(self) -> bool:
        # The retriever's BaseRetriever sets this; non-Base implementations
        # may not. Treat absent attribute as no expansion.
        return getattr(self.retriever, "expansion_enabled", False)

    async def retrieve_docs(
        self,
        partition: list[str],
        query: Query,
        top_k: int | None = None,
        filter_params: dict | None = None,
        trace: RetrievalTraceBuilder | None = None,
    ) -> list[Chunk]:
        """Run a single ``Query`` through retrieval, expansion, and reranking."""
        milvus_filter = query.to_milvus_filter()
        attempt_traces: list[RetrievalTraceBuilder] = []
        retrieval_trace = trace
        if trace is not None and milvus_filter and self.allow_filterless_fallback:
            retrieval_trace = RetrievalTraceBuilder(
                f"{trace.request_id}:temporal-filter",
                query.query,
                partition=partition[0] if len(partition) == 1 else None,
                attempt="temporal_filter",
                diagnostics=trace.diagnostics,
            )
            attempt_traces.append(retrieval_trace)
        trace_kwargs = {"trace": retrieval_trace} if retrieval_trace is not None else {}
        chunks = await self.retriever.retrieve(
            partition=partition,
            query=query.query,
            filter=milvus_filter,
            filter_params=filter_params,
            **trace_kwargs,
        )

        if not chunks and milvus_filter and self.allow_filterless_fallback:
            # Temporal filter killed every candidate — retry without it so
            # the user gets some results rather than none.
            fallback_trace = None
            if trace is not None:
                fallback_trace = RetrievalTraceBuilder(
                    f"{trace.request_id}:filterless-fallback",
                    query.query,
                    partition=partition[0] if len(partition) == 1 else None,
                    attempt="filterless_fallback",
                    diagnostics=trace.diagnostics,
                )
                attempt_traces.append(fallback_trace)
            fallback_trace_kwargs = {"trace": fallback_trace} if fallback_trace is not None else {}
            chunks = await self.retriever.retrieve(
                partition=partition,
                query=query.query,
                filter=None,
                filter_params=filter_params,
                **fallback_trace_kwargs,
            )

        if trace is not None and attempt_traces:
            _safe_trace(
                trace,
                "retrieval_attempts",
                lambda: merge_child_traces(trace, attempt_traces),
            )

        if not chunks:
            _safe_trace(
                trace,
                "final",
                lambda: trace.record_stage("final", status="complete", candidates=[]),
            )
            return chunks

        pre_rerank_chunks = list(chunks)
        if self.reranker_enabled:
            started = time.perf_counter()
            chunks = await _rerank_chunks(self.reranker, query.query, chunks)
            elapsed = time.perf_counter() - started
            if trace is not None:
                surviving_ids = {str(_chunk_key(chunk)) for chunk in chunks}

                def record_pre_rerank() -> None:
                    pre_candidates = _with_removal_reasons(
                        trace.project_chunks("pre_rerank", pre_rerank_chunks),
                        {str(_chunk_key(chunk)) for chunk in pre_rerank_chunks} - surviving_ids,
                        "reranker_top_n",
                        "Excluded by the reranker's hard top-n limit.",
                    )
                    trace.record_stage(
                        "pre_rerank",
                        status="complete",
                        candidates=pre_candidates,
                        candidate_count=len(pre_rerank_chunks),
                    )

                _safe_trace(
                    trace,
                    "pre_rerank",
                    record_pre_rerank,
                )
                _safe_trace(
                    trace,
                    "post_rerank",
                    lambda: trace.record_stage(
                        "post_rerank",
                        status="complete",
                        candidates=trace.project_chunks("post_rerank", chunks),
                        candidate_count=len(chunks),
                        duration_seconds=elapsed,
                    ),
                )
            _safe_trace(
                trace,
                "reranking",
                lambda: trace.timings.__setitem__("reranking", trace.timings.get("reranking", 0.0) + elapsed),
            )
        else:
            _safe_trace(
                trace,
                "pre_rerank",
                lambda: trace.record_stage(
                    "pre_rerank",
                    status="complete",
                    candidates=trace.project_chunks("pre_rerank", chunks),
                    candidate_count=len(chunks),
                ),
            )

        if self.expansion_enabled:
            limit = self.reranker_top_k if top_k is None else max(self.reranker_top_k, top_k)
            head = copy.deepcopy(chunks[:limit])
            expanded = await self.retriever.expand_search_results(results=head, filter_params=filter_params)
            if len(expanded) > len(head):
                chunks = expanded
                if self.reranker_enabled:
                    pre_rerank_chunks = list(chunks)
                    started = time.perf_counter()
                    chunks = await _rerank_chunks(self.reranker, query.query, chunks)
                    elapsed = time.perf_counter() - started
                    if trace is not None:
                        surviving_ids = {str(_chunk_key(chunk)) for chunk in chunks}

                        def record_expanded_pre_rerank() -> None:
                            pre_candidates = _with_removal_reasons(
                                trace.project_chunks("pre_rerank", pre_rerank_chunks),
                                {str(_chunk_key(chunk)) for chunk in pre_rerank_chunks} - surviving_ids,
                                "reranker_top_n",
                                "Excluded by the reranker's hard top-n limit.",
                            )
                            trace.record_stage(
                                "pre_rerank",
                                status="complete",
                                candidates=pre_candidates,
                                candidate_count=len(pre_rerank_chunks),
                            )

                        _safe_trace(
                            trace,
                            "pre_rerank",
                            record_expanded_pre_rerank,
                        )
                        _safe_trace(
                            trace,
                            "post_rerank",
                            lambda: trace.record_stage(
                                "post_rerank",
                                status="complete",
                                candidates=trace.project_chunks("post_rerank", chunks),
                                candidate_count=len(chunks),
                                duration_seconds=elapsed,
                            ),
                        )
                    _safe_trace(
                        trace,
                        "reranking",
                        lambda: trace.timings.__setitem__("reranking", trace.timings.get("reranking", 0.0) + elapsed),
                    )

        # `reranker_top_k` is NOT applied here as a final cutoff — only
        # `top_k` (an explicit caller-supplied value, e.g. map-reduce's
        # max_total_documents) truncates. On the common no-`top_k` chat path
        # this returns everything reranked (up to the retriever's own
        # top_k), which callers sizing a token budget off reranker_top_k
        # should not assume is bounded by it. Tracked separately:
        # https://github.com/linagora/openrag/issues/851
        if top_k is not None:
            removed_ids = {str(_chunk_key(chunk)) for chunk in chunks[top_k:]}
            stage_name = "post_rerank" if self.reranker_enabled else "pre_rerank"
            if removed_ids and trace is not None:
                _safe_trace(
                    trace,
                    stage_name,
                    lambda: trace.record_stage(
                        stage_name,
                        status="complete",
                        candidates=_with_removal_reasons(
                            list(trace.stages[stage_name].candidates),
                            removed_ids,
                            "final_top_n",
                            "Excluded by the final public result cutoff.",
                        ),
                        candidate_count=trace.stages[stage_name].candidate_count,
                        duration_seconds=trace.stages[stage_name].duration_seconds,
                    ),
                )
            chunks = chunks[:top_k]
        _safe_trace(
            trace,
            "final",
            lambda: trace.record_stage(
                "final",
                status="complete",
                candidates=trace.project_chunks("final", chunks),
                candidate_count=len(chunks),
            ),
        )
        return chunks

    async def get_relevant_docs(
        self,
        partition: list[str],
        search_queries: SearchQueries,
        top_k: int | None = None,
        filter_params: dict | None = None,
        trace: RetrievalTraceBuilder | None = None,
    ) -> list[Chunk]:
        """Run every sub-query in parallel and fuse the per-query rankings via RRF."""
        child_traces = (
            [
                RetrievalTraceBuilder(
                    f"{trace.request_id}:query:{index}",
                    query.query,
                    diagnostics=trace.diagnostics,
                )
                for index, query in enumerate(search_queries.query_list)
            ]
            if trace is not None and len(search_queries.query_list) > 1
            else None
        )
        tasks = [
            self.retrieve_docs(
                partition=partition,
                query=q,
                top_k=top_k,
                filter_params=filter_params,
                trace=child_traces[index] if child_traces is not None else trace,
            )
            for index, q in enumerate(search_queries.query_list)
        ]
        ranked_lists = await asyncio.gather(*tasks)
        if trace is not None and child_traces is not None:
            merge_child_traces(trace, child_traces)
        fusion_kwargs: dict[str, Any] = {"k": self.rrf_k}
        if top_k is not None:
            fusion_kwargs["top_k"] = top_k
        if trace is not None:
            fusion_kwargs["trace"] = trace
        fused = rrf_reranking(
            ranked_lists,
            key_fn=_chunk_key,
            trace_stage="multi_query_fused",
            **fusion_kwargs,
        )
        _safe_trace(
            trace,
            "final",
            lambda: trace.record_stage(
                "final",
                status="complete",
                candidates=trace.project_chunks("final", fused),
                candidate_count=len(fused),
            ),
        )
        return fused
