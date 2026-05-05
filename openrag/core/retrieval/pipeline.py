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

from openrag.core.models.chunk import Chunk
from openrag.core.models.query import Query, SearchQueries
from openrag.core.rerankers.reranker import Reranker
from openrag.core.retrieval.retriever import Retriever
from openrag.core.retrieval.rrf import rrf_reranking


def _chunk_key(c: Chunk) -> Any:
    """Identity key for fusion / dedup. Falls back to object id when missing."""
    return c.id or id(c)


async def _rerank_chunks(reranker: Reranker, query: str, chunks: list[Chunk]) -> list[Chunk]:
    """Reorder chunks via the Reranker ABC.

    The ABC scores text+query pairs and returns ``[(orig_index, score), ...]``;
    we look up the original chunk for each ranked index. Items the reranker
    drops are excluded.
    """
    if not chunks:
        return chunks
    ranking = await reranker.rerank(query=query, documents=[c.text for c in chunks], top_k=None)
    return [chunks[idx] for idx, _ in ranking]


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
    ) -> None:
        self.retriever = retriever
        self.reranker = reranker
        self.reranker_top_k = reranker_top_k
        self.allow_filterless_fallback = allow_filterless_fallback

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
    ) -> list[Chunk]:
        """Run a single ``Query`` through retrieval, expansion, and reranking."""
        milvus_filter = query.to_milvus_filter()
        chunks = await self.retriever.retrieve(
            partition=partition,
            query=query.query,
            filter=milvus_filter,
            filter_params=filter_params,
        )

        if not chunks and milvus_filter and self.allow_filterless_fallback:
            # Temporal filter killed every candidate — retry without it so
            # the user gets some results rather than none.
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
            expanded = await self.retriever.expand_search_results(results=head)
            if len(expanded) > len(head):
                chunks = expanded
                if self.reranker_enabled:
                    chunks = await _rerank_chunks(self.reranker, query.query, chunks)

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
        fused = rrf_reranking(ranked_lists, key_fn=_chunk_key)
        if top_k is not None:
            fused = fused[:top_k]
        return fused
