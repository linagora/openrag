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
        prompt_service: Any | None = None,
    ) -> None:
        self._searcher = searcher
        self._config = config
        self._legacy_reranker = reranker
        self._legacy_llm = llm
        self._searcher_factory = searcher_factory
        self._reranker_factory = reranker_factory
        self._llm_factory = llm_factory
        # Resolves a preset's hyde/multi_query prompt by name (named -> default ->
        # disk). Optional: when absent (e.g. unit tests, no DB), we fall back to
        # the on-disk seed via load_template_by_key, preserving prior behaviour.
        self._prompt_service = prompt_service
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
        template: str | None = None,
    ):
        # ``template`` is the already-resolved query-expansion prompt for this
        # strategy (resolved from the preset's *_prompt_name in _pipeline_for_partition).
        if rtype == "multiQuery":
            return MultiQueryRetriever(
                llm=llm,
                multi_query_template=template,
                k_queries=k_queries,
                **common,
            )
        if rtype == "hyde":
            return HyDeRetriever(
                llm=llm,
                hyde_template=template,
                combine=combine,
                **common,
            )
        return SingleRetriever(**common)

    async def _resolve_query_template(self, prompt_type: str, name: str | None, disk_key: str) -> str:
        """Resolve a query-side prompt (hyde / multi_query) to its text.

        Prefers the library (named preset prompt -> type default) via
        PromptService; falls back to the on-disk seed when no PromptService is
        wired (unit tests / DB-less runs), so behaviour matches the pre-DB path.
        """
        if self._prompt_service is not None:
            return await self._prompt_service.resolve_prompt(prompt_type, names=[name])
        return load_template_by_key(self._config.paths.prompts_dir, self._config.prompts, disk_key)

    def _partition_configs(self) -> dict[str, Any]:
        return getattr(self._config, "partitions", {}) or {}

    def _require_partition_config(self, partition: str):
        partitions = self._partition_configs()
        if partition not in partitions:
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
        return partitions[partition]

    def _legacy_retriever_value(self, name: str, default: Any) -> Any:
        return getattr(self._config.retriever, name, default)

    def _resolve_reranker(self, reranker_name: str | None, partition: str) -> Reranker | None:
        """Effective reranker for one partition's retrieval pipeline.

        Resolution order — mirrors ``QueryService._resolve_llm``:

        1. The partition's configured ``reranker`` preset, resolved fresh via
           the model-endpoint catalog factory so a rename/promotion of that
           endpoint takes effect immediately.
        2. The **catalog default** endpoint (``is_default=True``) when the
           partition sets no preset, or its preset name has gone stale (the
           endpoint was renamed/deleted after assignment — unlike
           ``chat_llm``, this field has no create/PATCH-time validation, so a
           stale name reaching here is expected, not a bug).
        3. The static reranker built at startup from ``settings.reranker``,
           only when no factory is wired (unit tests) or the catalog has no
           default reranker endpoint yet.

        The resolved endpoint name is always logged (at debug), including for
        the default, so "which reranker ran?" is answerable from the logs.
        """
        if self._reranker_factory is None:
            return self._legacy_reranker
        if reranker_name:
            try:
                reranker = self._reranker_factory(reranker_name)
            except KeyError:
                logger.bind(reranker=reranker_name, partition=partition).warning(
                    "Partition reranker preset not found in the model-endpoint catalog — "
                    "falling back to the default reranker"
                )
            else:
                logger.bind(reranker=reranker_name, partition=partition).debug(
                    "Reranking with the partition's reranker preset"
                )
                return reranker
        try:
            reranker = self._reranker_factory("default")
        except KeyError:
            pass
        else:
            logger.bind(reranker=self._default_reranker_name(), partition=partition).debug(
                "Reranking with the default reranker preset"
            )
            return reranker
        logger.bind(partition=partition).debug(
            "Reranking with the static default reranker (no catalog default endpoint)"
        )
        return self._legacy_reranker

    def _default_reranker_name(self) -> str:
        """Real endpoint name behind the catalog reranker ``"default"`` alias, for logging.

        Same identity-lookup trick as ``QueryService._default_llm_name``: the
        ``"default"`` alias config object is the *same* object as its real-named
        entry, so the name is recovered by identity. Returns ``"default"`` when
        it can't be resolved (e.g. the alias isn't populated yet).
        """
        rerankers = self._config.models.reranker
        default_cfg = rerankers.get("default")
        if default_cfg is not None:
            for name, cfg in rerankers.items():
                if name != "default" and cfg is default_cfg:
                    return name
        return "default"

    async def _pipeline_for_partition(self, partition: str) -> tuple[RetrieverPipeline, int | None]:
        # Callers only ever pass a concrete partition name — the "all" sentinel is
        # expanded to concrete keys by _pipeline_groups_for_partitions before this
        # runs. With no per-partition configs at all, fall back to the legacy pipeline.
        if not self._partition_configs():
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

        # Only the expansion strategies need a prompt; type="single" (the common
        # case) resolves nothing, so the DB is never touched on that path.
        template = None
        if rtype == "multiQuery":
            template = await self._resolve_query_template(
                "multi_query", pipeline_cfg.multi_query_prompt_name, "multi_query"
            )
        elif rtype == "hyde":
            template = await self._resolve_query_template("hyde", pipeline_cfg.hyde_prompt_name, "hyde")

        reranker = self._resolve_reranker(pipeline_cfg.reranker, partition) if pipeline_cfg.enable_reranker else None

        retriever = self._build_retriever(
            rtype=rtype,
            template=template,
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

    async def _pipeline_groups_for_partitions(
        self, partitions: list[str]
    ) -> list[tuple[list[str], RetrieverPipeline, int | None]]:
        configs = self._partition_configs()
        # Expand the "all" sentinel to concrete partitions so each is retrieved
        # with its own embedder and top_n, via the same per-partition fan-out the
        # named-partition path already uses (#708). Collapsing to self._pipeline
        # embedded the query with the deployment-default embedder — near-random
        # recall for any partition indexed with a different one — and dropped the
        # reranker top_n (its default_top_k was None). "all" only reaches this
        # layer post-authorization (a SUPER_ADMIN_MODE admin; regular users are
        # already expanded to their memberships upstream), so every hydrated
        # partition is in scope.
        if "all" in partitions and configs:
            partitions = list(configs.keys())
        elif not partitions or not configs:
            # Nothing to expand (no partitions exist yet) — keep the single
            # legacy pipeline; there is no per-partition config to honour.
            return [(["all"] if "all" in partitions else partitions, self._pipeline, None)]
        groups: list[tuple[list[str], RetrieverPipeline, int | None]] = []
        for partition in partitions:
            pipeline, default_top_k = await self._pipeline_for_partition(partition)
            groups.append(([partition], pipeline, default_top_k))
        return groups

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

    async def _gather_partition_groups(self, coros: list) -> list:
        """Await one coroutine per partition group, bounding concurrency.

        Small fan-outs (the common case: a handful of partitions) run fully
        parallel via a plain gather — no added overhead, byte-identical to the
        prior behaviour. Only a fan-out larger than ``max_partition_concurrency``
        (e.g. a SUPER_ADMIN_MODE ``openrag-all`` expanded to every partition) is
        throttled through a per-call semaphore, so one request cannot launch a
        partition-count-proportional flood of embed+Milvus calls (#708).

        The semaphore is per-call, not shared: it caps this request's own fan-out
        without coupling concurrent requests, and the caps compose safely across
        the ``retrieve_per_query`` → ``retrieve`` nesting (each inner call bounds
        its own leaves; the coroutines being awaited hold no permit while
        waiting for one, so there is no cross-level deadlock).
        """
        limit = self._config.retriever.max_partition_concurrency
        if len(coros) <= limit:
            return await asyncio.gather(*coros)

        semaphore = asyncio.Semaphore(limit)

        async def _bounded(coro):
            async with semaphore:
                return await coro

        return await asyncio.gather(*(_bounded(c) for c in coros))

    async def retrieve(
        self,
        *,
        partitions: list[str],
        query: Query,
        top_k: int | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        """Single ``Query`` through retrieve → expand → rerank."""
        groups = await self._pipeline_groups_for_partitions(partitions)
        ranked_lists = await self._gather_partition_groups(
            [
                pipeline.retrieve_docs(
                    partition=partition_group,
                    query=query,
                    top_k=top_k if top_k is not None else default_top_k,
                    filter_params=filter_params,
                )
                for partition_group, pipeline, default_top_k in groups
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
        groups = await self._pipeline_groups_for_partitions(partitions)
        ranked_lists = await self._gather_partition_groups(
            [
                pipeline.get_relevant_docs(
                    partition=partition_group,
                    search_queries=search_queries,
                    top_k=top_k if top_k is not None else default_top_k,
                    filter_params=filter_params,
                )
                for partition_group, pipeline, default_top_k in groups
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
