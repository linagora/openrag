"""RetrievalService — retrieval orchestration (Phase 8C.1).

Wraps the clean ``core.retrieval`` pipeline (strategy + optional reranker
+ related/ancestor expansion + RRF fusion). The legacy
``components/retriever.py`` and ``RetrieverPipeline`` were Phase-5 shims
over this same core; this service is the real composition seam.

Searcher backing (logged decision, Phase 8C): the core retriever talks
to a ``RetrievalSearcher`` port. The only implementation today is
``MilvusRayShim`` (Ray ``Vectordb`` actor — embeds + hybrid-searches
internally). Per the dev-workflow doc, Ray cleanup is Phase 9, and
orchestrators may call Ray actors *behind a port* during the Phase-8
shim. So the searcher is injected (Ray stays behind the port); this
file has no Ray remote-call and no Ray import (8H stays satisfied). A
clean ``VectorStore``-backed searcher replaces it in Phase 9.

Constructor deviates from the plan's prescribed
``(vector_store, embedder_factory, reranker_factory, llm_factory,
document_repo, config)`` for the same reason: with the Ray-shim searcher,
the vector store / embedder / document repo are unused (the shim does
embedding + related/ancestor itself). The container injects the already
built ``searcher`` / ``reranker`` / ``llm`` plus ``config``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.retrieval.pipeline import RetrieverPipeline
from core.retrieval.retriever import (
    HyDeRetriever,
    MultiQueryRetriever,
    SingleRetriever,
    _expand_with_related_chunks,
)
from core.retrieval.rrf import rrf_reranking
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from core.models.chunk import Chunk
    from core.models.query import Query, SearchQueries
    from core.rerankers.reranker import Reranker
    from core.retrieval.searcher import RetrievalSearcher

logger = get_logger()


def _chunk_key(c: Chunk):
    return c.id or id(c)


class RetrievalService:
    """Retrieval pipeline orchestration (search, single/multi retrieve)."""

    def __init__(
        self,
        *,
        searcher: RetrievalSearcher,
        reranker: Reranker | None,
        llm: LLM | None,
        config: Settings,
    ) -> None:
        self._searcher = searcher
        rcfg = config.retriever
        common = {
            "searcher": searcher,
            "top_k": rcfg.top_k,
            "similarity_threshold": rcfg.similarity_threshold,
            "with_surrounding_chunks": rcfg.with_surrounding_chunks,
            "include_related": rcfg.include_related,
            "include_ancestors": rcfg.include_ancestors,
            "related_limit": rcfg.related_limit,
            "max_ancestor_depth": rcfg.max_ancestor_depth,
        }
        rtype = rcfg.type
        if rtype == "multiQuery":
            from components.prompts import MULTI_QUERY_PROMPT

            retriever = MultiQueryRetriever(
                llm=llm,
                multi_query_template=MULTI_QUERY_PROMPT,
                k_queries=rcfg.k_queries,
                **common,
            )
        elif rtype == "hyde":
            from components.prompts import HYDE_PROMPT

            retriever = HyDeRetriever(
                llm=llm,
                hyde_template=HYDE_PROMPT,
                combine=rcfg.combine,
                **common,
            )
        else:
            retriever = SingleRetriever(**common)

        self._pipeline = RetrieverPipeline(
            retriever=retriever,
            reranker=reranker if config.reranker.enabled else None,
            reranker_top_k=config.reranker.top_k,
            allow_filterless_fallback=rcfg.allow_filterless_fallback,
        )
        logger.debug(
            "RetrievalService ready",
            retriever=rtype,
            reranker_enabled=config.reranker.enabled and reranker is not None,
        )

    # ------------------------------------------------------------------
    # Raw semantic search (powers routers/search.py — was indexer.asearch)
    # ------------------------------------------------------------------

    async def search(
        self,
        *,
        text: str,
        partitions: str | list[str],
        top_k: int,
        similarity_threshold: float,
        filter: str | None = None,
        filter_params: dict | None = None,
        include_related: bool = False,
        include_ancestors: bool = False,
        related_limit: int = 20,
        max_ancestor_depth: int | None = None,
    ) -> list[Chunk]:
        """One similarity search, then optional related/ancestor expansion.

        Faithful port of ``indexer.asearch`` + the legacy
        ``_expand_with_related_chunks``: a single ``searcher.search`` (no
        query generation / reranking / RRF — those belong to QueryService).
        """
        parts = [partitions] if isinstance(partitions, str) else list(partitions)
        chunks = await self._searcher.search(
            query=text,
            partition=parts,
            top_k=top_k,
            filter=filter,
            filter_params=filter_params,
            similarity_threshold=similarity_threshold,
            with_surrounding_chunks=True,
        )
        if include_related or include_ancestors:
            chunks = await _expand_with_related_chunks(
                searcher=self._searcher,
                results=chunks,
                include_related=include_related,
                include_ancestors=include_ancestors,
                related_limit=related_limit,
                max_ancestor_depth=max_ancestor_depth,
            )
        return chunks

    # ------------------------------------------------------------------
    # Pipeline retrieval (powers QueryService — 8C.2)
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        *,
        partitions: list[str],
        query: Query,
        top_k: int | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        """Single ``Query`` through retrieve → expand → rerank."""
        return await self._pipeline.retrieve_docs(
            partition=partitions,
            query=query,
            top_k=top_k,
            filter_params=filter_params,
        )

    async def retrieve_multi(
        self,
        *,
        partitions: list[str],
        search_queries: SearchQueries,
        top_k: int | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        """Every sub-query in parallel, fused with RRF."""
        return await self._pipeline.get_relevant_docs(
            partition=partitions,
            search_queries=search_queries,
            top_k=top_k,
            filter_params=filter_params,
        )

    async def retrieve_per_query(
        self,
        *,
        partitions: list[str],
        queries: list[Query],
        top_k: int | None = None,
        filter_params: dict | None = None,
    ) -> list[list[Chunk]]:
        """Per-sub-query ranked lists (NOT fused).

        QueryService's combined web-search path interleaves these with web
        searches concurrently, then fuses; exposing the un-fused lists
        lets it run one ``asyncio.gather`` over both.
        """
        return await asyncio.gather(
            *[
                self._pipeline.retrieve_docs(
                    partition=partitions,
                    query=q,
                    top_k=top_k,
                    filter_params=filter_params,
                )
                for q in queries
            ]
        )

    @staticmethod
    def fuse(doc_lists: list[list[Chunk]], top_k: int | None = None) -> list[Chunk]:
        """RRF-fuse per-query ranked lists (same fusion the pipeline uses)."""
        fused = rrf_reranking(doc_lists, key_fn=_chunk_key)
        return fused[:top_k] if top_k is not None else fused


__all__ = ["RetrievalService"]
