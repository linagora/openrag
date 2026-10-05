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
from typing import Any

from core.models.chunk import Chunk
from core.models.query import Query, SearchQueries
from core.models.retrieval_result import ScoredChunk
from core.observability import tracing
from core.rerankers.reranker import Reranker
from core.retrieval.retriever import Retriever
from core.retrieval.rrf import rrf_reranking


def _chunk_key(c: Chunk) -> Any:
    """Identity key for fusion / dedup. Falls back to object id when missing."""
    return c.id or id(c)


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
    with tracing.observe(
        "rerank-candidates",
        input={"query": query, "candidates": tracing.describe_chunks(chunks) if tracing.recording() else None},
        metadata={"reranker": type(reranker).__name__, "model": getattr(reranker, "_model", None)},
    ) as step:
        ranking = await reranker.rerank(query=query, documents=[c.text for c in chunks], top_k=None)
        reranked = [ScoredChunk.from_chunk(chunks[idx], rerank_score=score) for idx, score in ranking]
        if tracing.recording():
            kept = {idx for idx, _ in ranking}
            step.update(
                output=tracing.describe_chunks(reranked),
                metadata={"dropped_chunk_ids": [c.id for idx, c in enumerate(chunks) if idx not in kept]},
            )
        return reranked


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

    def describe(self) -> dict[str, Any]:
        """The settings that shape this pipeline's results, as recorded in a trace."""
        retriever = self.retriever
        return {
            "retriever": type(retriever).__name__,
            "top_k": getattr(retriever, "top_k", None),
            "similarity_threshold": getattr(retriever, "similarity_threshold", None),
            "with_surrounding_chunks": getattr(retriever, "with_surrounding_chunks", None),
            "include_related": getattr(retriever, "include_related", None),
            "include_ancestors": getattr(retriever, "include_ancestors", None),
            "k_queries": getattr(retriever, "k_queries", None),
            "hyde_combine": getattr(retriever, "combine", None),
            "reranker": type(self.reranker).__name__ if self.reranker else None,
            "reranker_model": getattr(self.reranker, "_model", None),
            "reranker_top_k": self.reranker_top_k,
            "rrf_k": self.rrf_k,
            "allow_filterless_fallback": self.allow_filterless_fallback,
        }

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
    ) -> list[Chunk]:
        """Run a single ``Query`` through retrieval, expansion, and reranking."""
        milvus_filter = query.to_milvus_filter()
        with tracing.observe(
            "retrieve-subquery",
            as_type="retriever",
            input={"query": query.query, "temporal_filter": milvus_filter, "top_k": top_k},
        ) as step:
            chunks = await self._retrieve_docs(partition, query, milvus_filter, top_k, filter_params)
            if tracing.recording():
                step.update(output=tracing.describe_chunks(chunks))
            return chunks

    async def _retrieve_docs(
        self,
        partition: list[str],
        query: Query,
        milvus_filter: str | None,
        top_k: int | None,
        filter_params: dict | None,
    ) -> list[Chunk]:
        chunks = await self.retriever.retrieve(
            partition=partition,
            query=query.query,
            filter=milvus_filter,
            filter_params=filter_params,
        )

        if not chunks and milvus_filter and self.allow_filterless_fallback:
            # Temporal filter killed every candidate — retry without it so
            # the user gets some results rather than none.
            tracing.event("drop-temporal-filter", input={"temporal_filter": milvus_filter})
            chunks = await self.retriever.retrieve(
                partition=partition,
                query=query.query,
                filter=None,
                filter_params=filter_params,
            )

        if not chunks:
            return chunks

        if self.reranker_enabled:
            chunks = await _rerank_chunks(self.reranker, query.query, chunks)

        if self.expansion_enabled:
            limit = self.reranker_top_k if top_k is None else max(self.reranker_top_k, top_k)
            head = copy.deepcopy(chunks[:limit])
            with tracing.observe("expand-related-chunks", as_type="retriever", input={"head": len(head)}) as step:
                expanded = await self.retriever.expand_search_results(results=head, filter_params=filter_params)
                if tracing.recording():
                    step.update(output=tracing.describe_chunks(expanded[len(head) :], with_text=False))
            if len(expanded) > len(head):
                chunks = expanded
                if self.reranker_enabled:
                    chunks = await _rerank_chunks(self.reranker, query.query, chunks)

        # `reranker_top_k` is NOT applied here as a final cutoff — only
        # `top_k` (an explicit caller-supplied value, e.g. map-reduce's
        # max_total_documents) truncates. On the common no-`top_k` chat path
        # this returns everything reranked (up to the retriever's own
        # top_k), which callers sizing a token budget off reranker_top_k
        # should not assume is bounded by it. Tracked separately:
        # https://github.com/linagora/openrag/issues/851
        if top_k is not None:
            chunks = chunks[:top_k]
        return chunks

    async def get_relevant_docs(
        self,
        partition: list[str],
        search_queries: SearchQueries,
        top_k: int | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        """Run every sub-query in parallel and fuse the per-query rankings via RRF."""
        tasks = [
            self.retrieve_docs(
                partition=partition,
                query=q,
                top_k=top_k,
                filter_params=filter_params,
            )
            for q in search_queries.query_list
        ]
        ranked_lists = await asyncio.gather(*tasks)
        if len(ranked_lists) == 1:
            fused = list(ranked_lists[0])
        else:
            with tracing.observe("fuse-subqueries", input={"lists": len(ranked_lists), "rrf_k": self.rrf_k}) as step:
                fused = rrf_reranking(ranked_lists, key_fn=_chunk_key, k=self.rrf_k)
                if tracing.recording():
                    step.update(output=tracing.describe_chunks(fused, with_text=False))
        if top_k is not None:
            fused = fused[:top_k]
        return fused
