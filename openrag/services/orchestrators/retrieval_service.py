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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.prompts import load_template_by_key
from core.retrieval.pipeline import RetrieverPipeline
from core.retrieval.retriever import (
    HyDeRetriever,
    MultiQueryRetriever,
    SingleRetriever,
    _expand_with_related_chunks,
)
from core.retrieval.rrf import rrf_reranking
from core.utils.exceptions import PartitionNotFoundError
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
        reranker: Reranker | None = None,
        llm: LLM | None = None,
        config: Settings,
        searcher_factory: Callable[[str], RetrievalSearcher] | None = None,
        reranker_factory: Callable[[str], Reranker] | None = None,
        llm_factory: Callable[[str], LLM] | None = None,
    ) -> None:
        self._searcher = searcher
        self._config = config
        self._legacy_reranker = reranker
        self._legacy_llm = llm
        self._searcher_factory = searcher_factory
        self._reranker_factory = reranker_factory
        self._llm_factory = llm_factory
        self._pipeline = self._build_legacy_pipeline(reranker=reranker, llm=llm)

        logger.debug(
            "RetrievalService ready",
            retriever=config.retriever.type,
            reranker_enabled=config.reranker.enabled and reranker is not None,
            partition_configs=len(getattr(config, "partitions", {}) or {}),
        )

    def _build_legacy_pipeline(self, *, reranker: Reranker | None, llm: LLM | None) -> RetrieverPipeline:
        config = self._config
        rcfg = config.retriever
        common = {
            "searcher": self._searcher,
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
            retriever = MultiQueryRetriever(
                llm=llm,
                multi_query_template=load_template_by_key(config.paths.prompts_dir, config.prompts, "multi_query"),
                k_queries=rcfg.k_queries,
                **common,
            )
        elif rtype == "hyde":
            retriever = HyDeRetriever(
                llm=llm,
                hyde_template=load_template_by_key(config.paths.prompts_dir, config.prompts, "hyde"),
                combine=rcfg.combine,
                **common,
            )
        else:
            retriever = SingleRetriever(**common)

        return RetrieverPipeline(
            retriever=retriever,
            reranker=reranker if config.reranker.enabled else None,
            reranker_top_k=config.reranker.top_k,
            allow_filterless_fallback=rcfg.allow_filterless_fallback,
        )

    def _build_retriever(
        self,
        *,
        rtype: str,
        common: dict[str, Any],
        llm: LLM | None,
        k_queries: int,
        combine: bool,
    ):
        if rtype == "multiQuery":
            return MultiQueryRetriever(
                llm=llm,
                multi_query_template=load_template_by_key(
                    self._config.paths.prompts_dir,
                    self._config.prompts,
                    "multi_query",
                ),
                k_queries=k_queries,
                **common,
            )
        if rtype == "hyde":
            return HyDeRetriever(
                llm=llm,
                hyde_template=load_template_by_key(self._config.paths.prompts_dir, self._config.prompts, "hyde"),
                combine=combine,
                **common,
            )
        return SingleRetriever(**common)

    def _partition_configs(self) -> dict[str, Any]:
        return getattr(self._config, "partitions", {}) or {}

    def _require_partition_config(self, partition: str):
        partitions = self._partition_configs()
        if partition not in partitions:
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
        return partitions[partition]

    def _legacy_retriever_value(self, name: str, default: Any) -> Any:
        return getattr(self._config.retriever, name, default)

    def _pipeline_for_partition(self, partition: str) -> tuple[RetrieverPipeline, int | None]:
        if partition == "all" or not self._partition_configs():
            return self._pipeline, None

        partition_cfg = self._require_partition_config(partition)
        pipeline_cfg = partition_cfg.retrieval
        searcher = (
            self._searcher_factory(partition_cfg.embedder) if self._searcher_factory is not None else self._searcher
        )

        rtype = pipeline_cfg.type
        llm = self._legacy_llm
        if rtype in {"multiQuery", "hyde"} and self._llm_factory is not None:
            llm = self._llm_factory(pipeline_cfg.llm or partition_cfg.chat_llm or "default")

        reranker = None
        if pipeline_cfg.enable_reranker:
            if self._reranker_factory is not None:
                reranker = self._reranker_factory(pipeline_cfg.reranker or "default")
            else:
                reranker = self._legacy_reranker

        retriever = self._build_retriever(
            rtype=rtype,
            common={
                "searcher": searcher,
                "top_k": pipeline_cfg.top_k,
                "similarity_threshold": pipeline_cfg.similarity_threshold,
                "with_surrounding_chunks": self._legacy_retriever_value("with_surrounding_chunks", False),
                "include_related": pipeline_cfg.include_related,
                "include_ancestors": pipeline_cfg.include_ancestors,
                "related_limit": self._legacy_retriever_value("related_limit", 10),
                "max_ancestor_depth": self._legacy_retriever_value("max_ancestor_depth", None),
            },
            llm=llm,
            k_queries=self._legacy_retriever_value("k_queries", 3),
            combine=self._legacy_retriever_value("combine", False),
        )
        pipeline = RetrieverPipeline(
            retriever=retriever,
            reranker=reranker,
            reranker_top_k=pipeline_cfg.top_n,
            allow_filterless_fallback=self._legacy_retriever_value("allow_filterless_fallback", True),
            rrf_k=pipeline_cfg.rrf_k,
        )
        return pipeline, pipeline_cfg.top_n

    def _pipeline_groups_for_partitions(
        self, partitions: list[str]
    ) -> list[tuple[list[str], RetrieverPipeline, int | None]]:
        if not partitions or "all" in partitions or not self._partition_configs():
            return [(["all"] if "all" in partitions else partitions, self._pipeline, None)]
        return [
            ([partition], pipeline, default_top_k)
            for partition in partitions
            for pipeline, default_top_k in [self._pipeline_for_partition(partition)]
        ]

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
                filter_params=filter_params,
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
        ranked_lists = await asyncio.gather(
            *[
                pipeline.retrieve_docs(
                    partition=partition_group,
                    query=query,
                    top_k=top_k if top_k is not None else default_top_k,
                    filter_params=filter_params,
                )
                for partition_group, pipeline, default_top_k in self._pipeline_groups_for_partitions(partitions)
            ]
        )
        return ranked_lists[0] if len(ranked_lists) == 1 else self.fuse(ranked_lists, top_k=top_k)

    async def retrieve_multi(
        self,
        *,
        partitions: list[str],
        search_queries: SearchQueries,
        top_k: int | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        """Every sub-query in parallel, fused with RRF."""
        ranked_lists = await asyncio.gather(
            *[
                pipeline.get_relevant_docs(
                    partition=partition_group,
                    search_queries=search_queries,
                    top_k=top_k if top_k is not None else default_top_k,
                    filter_params=filter_params,
                )
                for partition_group, pipeline, default_top_k in self._pipeline_groups_for_partitions(partitions)
            ]
        )
        return ranked_lists[0] if len(ranked_lists) == 1 else self.fuse(ranked_lists, top_k=top_k)

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
            *[self.retrieve(partitions=partitions, query=q, top_k=top_k, filter_params=filter_params) for q in queries]
        )

    @staticmethod
    def fuse(doc_lists: list[list[Chunk]], top_k: int | None = None) -> list[Chunk]:
        """RRF-fuse ranked lists across partitions (and doc+web).

        Uses the canonical RRF constant (60) rather than a preset's ``rrf_k``:
        this fuses lists from *different* partitions (and the web branch), so no
        single partition's ``rrf_k`` applies. Per-partition ``rrf_k`` is honoured
        one layer down, in ``RetrieverPipeline.get_relevant_docs`` (#707).
        """
        fused = rrf_reranking(doc_lists, key_fn=_chunk_key)
        return fused[:top_k] if top_k is not None else fused


__all__ = ["RetrievalService"]
