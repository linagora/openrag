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
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.models.preset import resolve_partition_chat_llm
from core.prompts import load_template_by_key
from core.retrieval.pipeline import RetrieverPipeline
from core.retrieval.retriever import (
    HyDeRetriever,
    MultiQueryRetriever,
    SingleRetriever,
    _expand_with_related_chunks,
)
from core.retrieval.rrf import rrf_reranking
from core.retrieval.trace import RetrievalTraceBuilder, canonical_fingerprint
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


@dataclass(frozen=True, slots=True)
class ResolvedRetrievalGroup:
    partitions: tuple[str, ...]
    pipeline: RetrieverPipeline
    default_top_k: int | None


@dataclass(frozen=True, slots=True)
class ResolvedRetrievalPlan:
    groups: tuple[ResolvedRetrievalGroup, ...]
    public_configuration: dict[str, object]
    configuration_fingerprint: str


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

    def _build_legacy_pipeline(
        self,
        *,
        reranker: Reranker | None,
        llm: LLM | None,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
    ) -> RetrieverPipeline:
        config = self._config
        rcfg = config.retriever
        common = {
            "searcher": self._searcher,
            "top_k": top_k if top_k is not None else rcfg.top_k,
            "similarity_threshold": (
                similarity_threshold if similarity_threshold is not None else rcfg.similarity_threshold
            ),
            "with_surrounding_chunks": rcfg.with_surrounding_chunks,
            "include_related": False if disable_expansion else rcfg.include_related,
            "include_ancestors": False if disable_expansion else rcfg.include_ancestors,
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
            reranker=reranker if config.reranker.enabled and not disable_reranker else None,
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

    async def _resolve_query_prompt(
        self,
        prompt_type: str,
        name: str | None,
        disk_key: str,
    ) -> tuple[str, dict[str, str | None]]:
        """Resolve a query-side prompt to its text and content-free identity.

        Prefers the library (named preset prompt -> type default) via
        PromptService; falls back to the on-disk seed when no PromptService is
        wired (unit tests / DB-less runs), so behaviour matches the pre-DB path.
        """
        resolve_with_identity = getattr(self._prompt_service, "resolve_prompt_with_identity", None)
        if resolve_with_identity is not None:
            resolved = await resolve_with_identity(prompt_type, names=[name])
            return resolved.content, self._public_prompt_identity(resolved)
        if self._prompt_service is not None:
            content = await self._prompt_service.resolve_prompt(prompt_type, names=[name])
            name = None
            source = None
        else:
            content = load_template_by_key(self._config.paths.prompts_dir, self._config.prompts, disk_key)
            source = "disk-seed"
            name = None
        return content, {
            "name": name,
            "source": source,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        }

    async def _resolve_query_template(self, prompt_type: str, name: str | None, disk_key: str) -> str:
        content, _identity = await self._resolve_query_prompt(prompt_type, name, disk_key)
        return content

    def _partition_configs(self) -> dict[str, Any]:
        return getattr(self._config, "partitions", {}) or {}

    def _effective_contextualizer_name(self, partitions: Sequence[str]) -> str:
        """Mirror QueryService's partition LLM resolution for public metadata."""
        if self._llm_factory is None:
            return "default"
        name = resolve_partition_chat_llm(list(partitions), self._partition_configs())
        registered = getattr(getattr(self._config, "models", None), "llm", {}) or {}
        return name if name is not None and name in registered else "default"

    @staticmethod
    def _public_endpoint(config: Settings, endpoint_type: str, name: str | None) -> dict[str, object]:
        """Return endpoint identity only, excluding URLs, keys, and arbitrary extras."""
        registry = getattr(getattr(config, "models", None), endpoint_type, {}) or {}
        endpoint = registry.get(name) if name is not None else None
        return {
            "name": name,
            "model": getattr(endpoint, "model_name", None),
        }

    def public_retrieval_configuration(self, partitions: Sequence[str]) -> dict[str, object]:
        """Build the stable public retrieval-setting subset used for fingerprints."""
        configured_partitions = self._partition_configs()
        public_partitions: list[dict[str, object]] = []
        selected_partitions = (
            list(configured_partitions) if "all" in partitions and configured_partitions else partitions
        )
        contextualizer_name = self._effective_contextualizer_name(partitions)
        llm_registry = getattr(getattr(self._config, "models", None), "llm", {}) or {}
        contextualizer_endpoint = (
            {
                "name": "default",
                "model": getattr(getattr(self._config, "llm", None), "model", None),
            }
            if self._llm_factory is None or (contextualizer_name == "default" and "default" not in llm_registry)
            else self._public_endpoint(self._config, "llm", contextualizer_name)
        )
        for partition_name in sorted(set(selected_partitions)):
            partition = configured_partitions.get(partition_name)
            retrieval = partition.retrieval if partition is not None else self._config.retriever
            embedder_name = partition.embedder if partition is not None else "default"
            query_expansion_llm = None
            if getattr(retrieval, "type", None) in {"multiQuery", "hyde"}:
                if self._llm_factory is None:
                    query_expansion_llm = {
                        "name": "default",
                        "model": getattr(getattr(self._config, "llm", None), "model", None),
                    }
                else:
                    query_expansion_llm = self._public_endpoint(
                        self._config,
                        "llm",
                        getattr(retrieval, "llm", None)
                        or (getattr(partition, "chat_llm", None) if partition is not None else None)
                        or "default",
                    )
            reranker_enabled = bool(
                getattr(
                    retrieval,
                    "enable_reranker",
                    getattr(getattr(self._config, "reranker", None), "enabled", False),
                )
            )
            reranker_name = getattr(retrieval, "reranker", None) or ("default" if reranker_enabled else None)
            related_limit = getattr(
                retrieval,
                "related_limit",
                self._legacy_retriever_value("related_limit", 10),
            )
            max_ancestor_depth = getattr(
                retrieval,
                "max_ancestor_depth",
                self._legacy_retriever_value("max_ancestor_depth", None),
            )
            public_partitions.append(
                {
                    "name": partition_name,
                    "embedder": self._public_endpoint(self._config, "embedder", embedder_name),
                    "retrieval": {
                        "type": getattr(retrieval, "type", None),
                        "top_k": getattr(retrieval, "top_k", None),
                        "similarity_threshold": getattr(retrieval, "similarity_threshold", None),
                        "rrf_k": getattr(retrieval, "rrf_k", 60),
                        "k_queries": self._legacy_retriever_value("k_queries", 3),
                        "combine": self._legacy_retriever_value("combine", False),
                        "with_surrounding_chunks": self._legacy_retriever_value("with_surrounding_chunks", False),
                        "allow_filterless_fallback": self._legacy_retriever_value("allow_filterless_fallback", True),
                        "hyde_prompt_name": getattr(retrieval, "hyde_prompt_name", None),
                        "multi_query_prompt_name": getattr(retrieval, "multi_query_prompt_name", None),
                        "query_expansion_llm": query_expansion_llm,
                    },
                    "reranker": {
                        **self._public_endpoint(self._config, "reranker", reranker_name),
                        "enabled": reranker_enabled,
                        "top_n": getattr(
                            retrieval,
                            "top_n",
                            getattr(getattr(self._config, "reranker", None), "top_k", None),
                        ),
                    },
                    "contextualizer": {
                        **contextualizer_endpoint,
                        "prompt_name": getattr(retrieval, "query_contextualizer_prompt_name", None),
                    },
                    "expansion": {
                        "include_related": getattr(retrieval, "include_related", False),
                        "include_ancestors": getattr(retrieval, "include_ancestors", False),
                        "related_limit": related_limit,
                        "max_ancestor_depth": max_ancestor_depth,
                    },
                }
            )
        hybrid_enabled = getattr(getattr(self._config, "vectordb", None), "hybrid_search", None)
        return {
            "hybrid": {
                "enabled": hybrid_enabled,
                "fusion": "rrf" if hybrid_enabled else None,
            },
            "partitions": public_partitions,
        }

    def configuration_fingerprint(self, partitions: Sequence[str]) -> str:
        """Fingerprint only allowlisted public settings for the authorized scope."""
        return canonical_fingerprint(self.public_retrieval_configuration(partitions))

    def public_search_configuration(
        self,
        partitions: Sequence[str],
        effective_options: Mapping[str, object],
    ) -> dict[str, object]:
        """Return only settings that can affect the raw search endpoint."""
        configured_partitions = self._partition_configs()
        selected_partitions = (
            list(configured_partitions) if "all" in partitions and configured_partitions else partitions
        )
        default_embedder = (getattr(getattr(self._config, "models", None), "embedder", {}) or {}).get("default")
        raw_search_embedder = {
            "name": "default",
            "model": getattr(getattr(self._config, "embedder", None), "model_name", None),
            "vector_field": getattr(default_embedder, "vector_field", None),
        }
        public_partitions = [
            {"name": partition_name, "embedder": raw_search_embedder}
            for partition_name in sorted(set(selected_partitions))
        ]
        hybrid_enabled = getattr(getattr(self._config, "vectordb", None), "hybrid_search", None)
        return {
            "operation": "raw_search",
            "hybrid": {
                "enabled": hybrid_enabled,
                "fusion": "rrf" if hybrid_enabled else None,
            },
            "partitions": public_partitions,
            "request": dict(effective_options),
        }

    def search_configuration_fingerprint(
        self,
        partitions: Sequence[str],
        effective_options: Mapping[str, object],
    ) -> str:
        """Fingerprint the effective raw-search path without chat-only settings."""
        return canonical_fingerprint(self.public_search_configuration(partitions, effective_options))

    def _contextualizer_prompt_name(self, partitions: Sequence[str]) -> str | None:
        selected = list(dict.fromkeys(partitions))
        if len(selected) != 1 or "all" in selected:
            return None
        partition = self._partition_configs().get(selected[0])
        return getattr(getattr(partition, "retrieval", None), "query_contextualizer_prompt_name", None)

    async def _contextualizer_prompt_identity(self, partitions: Sequence[str]) -> dict[str, str | None]:
        prompt_name = self._contextualizer_prompt_name(partitions)
        resolve_with_identity = getattr(self._prompt_service, "resolve_prompt_with_identity", None)
        if resolve_with_identity is not None:
            resolved = await resolve_with_identity("query_contextualizer", names=[prompt_name])
            return {
                "name": resolved.name,
                "source": resolved.source,
                "content_hash": resolved.content_hash,
            }

        if self._prompt_service is not None:
            content = await self._prompt_service.resolve_prompt("query_contextualizer", names=[prompt_name])
            prompt_name = None
            source = None
        else:
            content = load_template_by_key(
                self._config.paths.prompts_dir,
                self._config.prompts,
                "query_contextualizer",
            )
            source = "disk-seed"
            prompt_name = None
        return {
            "name": prompt_name,
            "source": source,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        }

    @staticmethod
    def _public_prompt_identity(prompt: object) -> dict[str, str | None]:
        def field(key: str) -> str | None:
            return prompt.get(key) if isinstance(prompt, Mapping) else getattr(prompt, key, None)

        return {
            "name": field("name"),
            "source": field("source"),
            "content_hash": field("content_hash"),
        }

    async def _query_expansion_prompt_identity(self, retrieval: object) -> dict[str, str | None] | None:
        retrieval_type = getattr(retrieval, "type", None)
        if retrieval_type == "hyde":
            prompt_type = "hyde"
            prompt_name = getattr(retrieval, "hyde_prompt_name", None)
        elif retrieval_type == "multiQuery":
            prompt_type = "multi_query"
            prompt_name = getattr(retrieval, "multi_query_prompt_name", None)
        else:
            return None
        _content, identity = await self._resolve_query_prompt(prompt_type, prompt_name, prompt_type)
        return {"type": prompt_type, **identity}

    async def resolved_public_retrieval_configuration(
        self,
        partitions: Sequence[str],
        *,
        contextualizer_prompt: object | None = None,
    ) -> dict[str, object]:
        """Return the same resolved public settings used to build execution."""
        plan = await self.resolve_retrieval_plan(
            partitions,
            contextualizer_prompt=contextualizer_prompt,
            build_execution=False,
        )
        return plan.public_configuration

    async def resolved_configuration_fingerprint(
        self,
        partitions: Sequence[str],
        *,
        contextualizer_prompt: object | None = None,
    ) -> str:
        """Fingerprint public retrieval settings and the resolved prompt identity."""
        plan = await self.resolve_retrieval_plan(
            partitions,
            contextualizer_prompt=contextualizer_prompt,
            build_execution=False,
        )
        return plan.configuration_fingerprint

    def _require_partition_config(self, partition: str):
        partitions = self._partition_configs()
        if partition not in partitions:
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
        return partitions[partition]

    def _legacy_retriever_value(self, name: str, default: Any) -> Any:
        return getattr(self._config.retriever, name, default)

    def _resolve_reranker_with_identity(
        self,
        reranker_name: str | None,
        partition: str,
    ) -> tuple[Reranker | None, dict[str, object]]:
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
            return self._legacy_reranker, {
                **self._public_endpoint(self._config, "reranker", "default"),
                "name": "default" if self._legacy_reranker is not None else None,
            }
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
                return reranker, self._public_endpoint(self._config, "reranker", reranker_name)
        try:
            reranker = self._reranker_factory("default")
        except KeyError:
            pass
        else:
            logger.bind(reranker=self._default_reranker_name(), partition=partition).debug(
                "Reranking with the default reranker preset"
            )
            return reranker, self._public_endpoint(self._config, "reranker", "default")
        logger.bind(partition=partition).debug(
            "Reranking with the static default reranker (no catalog default endpoint)"
        )
        return self._legacy_reranker, {
            **self._public_endpoint(self._config, "reranker", "default"),
            "name": "default" if self._legacy_reranker is not None else None,
        }

    def _resolve_reranker(self, reranker_name: str | None, partition: str) -> Reranker | None:
        reranker, _identity = self._resolve_reranker_with_identity(reranker_name, partition)
        return reranker

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

    async def _resolved_pipeline_for_partition(
        self,
        partition: str,
        *,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
    ) -> tuple[RetrieverPipeline, int | None, dict[str, str | None] | None, dict[str, object] | None]:
        # Callers only ever pass a concrete partition name — the "all" sentinel is
        # expanded to concrete keys by _pipeline_groups_for_partitions before this
        # runs. With no per-partition configs at all, fall back to the legacy pipeline.
        if not self._partition_configs():
            return self._pipeline, None, self._legacy_query_expansion_prompt_identity(self._pipeline), None

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
        prompt_identity = None
        if rtype == "multiQuery":
            template, prompt_identity = await self._resolve_query_prompt(
                "multi_query", pipeline_cfg.multi_query_prompt_name, "multi_query"
            )
            prompt_identity = {"type": "multi_query", **prompt_identity}
        elif rtype == "hyde":
            template, prompt_identity = await self._resolve_query_prompt("hyde", pipeline_cfg.hyde_prompt_name, "hyde")
            prompt_identity = {"type": "hyde", **prompt_identity}

        reranker = None
        reranker_identity = None
        if pipeline_cfg.enable_reranker and not disable_reranker:
            reranker, reranker_identity = self._resolve_reranker_with_identity(pipeline_cfg.reranker, partition)

        retriever = self._build_retriever(
            rtype=rtype,
            template=template,
            common={
                "searcher": searcher,
                "top_k": top_k if top_k is not None else pipeline_cfg.top_k,
                "similarity_threshold": (
                    similarity_threshold if similarity_threshold is not None else pipeline_cfg.similarity_threshold
                ),
                "with_surrounding_chunks": self._legacy_retriever_value("with_surrounding_chunks", False),
                "include_related": False if disable_expansion else pipeline_cfg.include_related,
                "include_ancestors": False if disable_expansion else pipeline_cfg.include_ancestors,
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
        return pipeline, pipeline_cfg.top_n, prompt_identity, reranker_identity

    @staticmethod
    def _legacy_query_expansion_prompt_identity(pipeline: RetrieverPipeline) -> dict[str, str | None] | None:
        retriever = pipeline.retriever
        if isinstance(retriever, MultiQueryRetriever):
            prompt_type = "multi_query"
            content = retriever.multi_query_template
        elif isinstance(retriever, HyDeRetriever):
            prompt_type = "hyde"
            content = retriever.hyde_template
        else:
            return None
        return {
            "type": prompt_type,
            "name": None,
            "source": "disk-seed",
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        }

    async def _pipeline_for_partition(
        self,
        partition: str,
        *,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
    ) -> tuple[RetrieverPipeline, int | None]:
        pipeline, default_top_k, _prompt_identity, _reranker_identity = await self._resolved_pipeline_for_partition(
            partition,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            disable_reranker=disable_reranker,
            disable_expansion=disable_expansion,
        )
        return pipeline, default_top_k

    async def resolve_retrieval_plan(
        self,
        partitions: Sequence[str],
        *,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
        contextualizer_prompt: object | None = None,
        build_execution: bool = True,
    ) -> ResolvedRetrievalPlan:
        """Resolve executable pipelines and their public identity once."""
        requested = list(partitions)
        configs = self._partition_configs()
        selected = list(configs) if "all" in requested and configs else list(dict.fromkeys(requested))
        prompt_identities: dict[str, dict[str, str | None] | None] = {}
        reranker_identities: dict[str, dict[str, object]] = {}
        groups: list[ResolvedRetrievalGroup] = []

        if not build_execution:
            for partition_name in selected:
                partition = configs.get(partition_name)
                retrieval = partition.retrieval if partition is not None else self._config.retriever
                prompt_identities[partition_name] = (
                    await self._query_expansion_prompt_identity(retrieval)
                    if configs
                    else self._legacy_query_expansion_prompt_identity(self._pipeline)
                )
                if getattr(retrieval, "enable_reranker", False) and not disable_reranker:
                    _reranker, identity = self._resolve_reranker_with_identity(
                        getattr(retrieval, "reranker", None),
                        partition_name,
                    )
                    reranker_identities[partition_name] = identity
        elif not selected or not configs:
            pipeline = self._pipeline
            if any((top_k is not None, similarity_threshold is not None, disable_reranker, disable_expansion)):
                pipeline = self._build_legacy_pipeline(
                    reranker=self._legacy_reranker,
                    llm=self._legacy_llm,
                    top_k=top_k,
                    similarity_threshold=similarity_threshold,
                    disable_reranker=disable_reranker,
                    disable_expansion=disable_expansion,
                )
            group_partitions = tuple(["all"] if "all" in requested else selected)
            groups.append(ResolvedRetrievalGroup(group_partitions, pipeline, None))
            legacy_identity = self._legacy_query_expansion_prompt_identity(pipeline)
            for partition_name in selected:
                prompt_identities[partition_name] = legacy_identity
        else:
            for partition_name in selected:
                (
                    pipeline,
                    default_top_k,
                    prompt_identity,
                    reranker_identity,
                ) = await self._resolved_pipeline_for_partition(
                    partition_name,
                    top_k=top_k,
                    similarity_threshold=similarity_threshold,
                    disable_reranker=disable_reranker,
                    disable_expansion=disable_expansion,
                )
                groups.append(ResolvedRetrievalGroup((partition_name,), pipeline, default_top_k))
                prompt_identities[partition_name] = prompt_identity
                if reranker_identity is not None:
                    reranker_identities[partition_name] = reranker_identity

        public = self.public_retrieval_configuration(requested)
        for public_partition in public["partitions"]:
            public_retrieval = public_partition["retrieval"]
            partition_name = public_partition["name"]
            public_retrieval["query_expansion_prompt"] = prompt_identities.get(partition_name)
            if partition_name in reranker_identities:
                public_partition["reranker"].update(reranker_identities[partition_name])
            if top_k is not None:
                public_retrieval["top_k"] = top_k
            if similarity_threshold is not None:
                public_retrieval["similarity_threshold"] = similarity_threshold
            if disable_reranker:
                public_partition["reranker"]["enabled"] = False
            if disable_expansion:
                public_partition["expansion"]["include_related"] = False
                public_partition["expansion"]["include_ancestors"] = False
        public["contextualizer_prompt"] = (
            self._public_prompt_identity(contextualizer_prompt)
            if contextualizer_prompt is not None
            else await self._contextualizer_prompt_identity(requested)
        )
        return ResolvedRetrievalPlan(
            groups=tuple(groups),
            public_configuration=public,
            configuration_fingerprint=canonical_fingerprint(public),
        )

    async def _pipeline_groups_for_partitions(
        self,
        partitions: list[str],
        *,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
    ) -> list[tuple[list[str], RetrieverPipeline, int | None]]:
        configs = self._partition_configs()
        if "all" in partitions and configs:
            partitions = list(configs.keys())
        elif not partitions or not configs:
            pipeline = self._pipeline
            if any((top_k is not None, similarity_threshold is not None, disable_reranker, disable_expansion)):
                pipeline = self._build_legacy_pipeline(
                    reranker=self._legacy_reranker,
                    llm=self._legacy_llm,
                    top_k=top_k,
                    similarity_threshold=similarity_threshold,
                    disable_reranker=disable_reranker,
                    disable_expansion=disable_expansion,
                )
            return [(["all"] if "all" in partitions else partitions, pipeline, None)]
        groups: list[tuple[list[str], RetrieverPipeline, int | None]] = []
        for partition in partitions:
            pipeline, default_top_k = await self._pipeline_for_partition(
                partition,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
            )
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
        trace: RetrievalTraceBuilder | None = None,
    ) -> list[Chunk]:
        """One similarity search, then optional related/ancestor expansion.

        Faithful port of ``indexer.asearch`` + the legacy
        ``_expand_with_related_chunks``: a single ``searcher.search`` (no
        query generation / reranking / RRF — those belong to QueryService).
        """
        parts = [partitions] if isinstance(partitions, str) else list(partitions)
        if trace is not None:
            trace.record_stage("original_query", status="complete", candidates=[])
        trace_kwargs = {"trace": trace} if trace is not None else {}
        chunks = await self._searcher.search(
            query=text,
            partition=parts,
            top_k=top_k,
            filter=filter,
            filter_params=filter_params,
            similarity_threshold=similarity_threshold,
            with_surrounding_chunks=True,
            **trace_kwargs,
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
        if trace is not None:
            try:
                trace.record_stage(
                    "final",
                    status="complete",
                    candidates=trace.project_chunks("final", chunks),
                    candidate_count=len(chunks),
                )
            except Exception as error:
                trace.record_error("final", error)
        return chunks

    # ------------------------------------------------------------------
    # Pipeline retrieval (powers QueryService — 8C.2)
    # ------------------------------------------------------------------

    async def _gather_partition_groups(self, legs: list[tuple[list[str], Awaitable[list]]]) -> list:
        """Await one coroutine per partition group, bounding concurrency.

        Each leg is ``(partition_names, coroutine)``; the names are carried so a
        dropped leg can be named in the log.

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

        One unhealthy partition must not empty the whole result set (#736), so
        legs are gathered with ``return_exceptions=True`` and a failed one is
        dropped with a warning naming it. Two cases are deliberately not
        degraded: ``CancelledError`` is re-raised so a client disconnect or a
        timeout still unwinds, and if *every* leg failed the first error is
        re-raised — an empty list is indistinguishable from "no match" and would
        answer from no context instead of surfacing the outage.
        """
        limit = self._config.retriever.max_partition_concurrency
        coros = [coro for _, coro in legs]
        if len(coros) > limit:
            semaphore = asyncio.Semaphore(limit)

            async def _bounded(coro):
                async with semaphore:
                    return await coro

            coros = [_bounded(c) for c in coros]
        results = await asyncio.gather(*coros, return_exceptions=True)

        ranked_lists = []
        first_error: BaseException | None = None
        for (partition_names, _), result in zip(legs, results, strict=True):
            if not isinstance(result, BaseException):
                ranked_lists.append(result)
                continue
            if isinstance(result, asyncio.CancelledError):
                raise result
            if first_error is None:
                first_error = result
            logger.bind(partitions=partition_names).warning(
                f"Retrieval degraded: dropping partition(s) {partition_names} — {type(result).__name__}: {result}"
            )

        if first_error is not None and not ranked_lists:
            raise first_error
        return ranked_lists

    async def retrieve(
        self,
        *,
        partitions: list[str],
        query: Query,
        top_k: int | None = None,
        retrieval_top_k: int | None = None,
        filter_params: dict | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
        resolved_plan: ResolvedRetrievalPlan | None = None,
    ) -> list[Chunk]:
        """Single ``Query`` through retrieve → expand → rerank."""
        if resolved_plan is None:
            groups = await self._pipeline_groups_for_partitions(
                partitions,
                top_k=retrieval_top_k,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
            )
        else:
            groups = [(list(group.partitions), group.pipeline, group.default_top_k) for group in resolved_plan.groups]
        ranked_lists = await self._gather_partition_groups(
            [
                (
                    partition_group,
                    pipeline.retrieve_docs(
                        partition=partition_group,
                        query=query,
                        top_k=top_k if top_k is not None else default_top_k,
                        filter_params=filter_params,
                    ),
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
        retrieval_top_k: int | None = None,
        filter_params: dict | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
        resolved_plan: ResolvedRetrievalPlan | None = None,
    ) -> list[Chunk]:
        """Every sub-query in parallel, fused with RRF."""
        if resolved_plan is None:
            groups = await self._pipeline_groups_for_partitions(
                partitions,
                top_k=retrieval_top_k,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
            )
        else:
            groups = [(list(group.partitions), group.pipeline, group.default_top_k) for group in resolved_plan.groups]
        ranked_lists = await self._gather_partition_groups(
            [
                (
                    partition_group,
                    pipeline.get_relevant_docs(
                        partition=partition_group,
                        search_queries=search_queries,
                        top_k=top_k if top_k is not None else default_top_k,
                        filter_params=filter_params,
                    ),
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
        retrieval_top_k: int | None = None,
        filter_params: dict | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
        resolved_plan: ResolvedRetrievalPlan | None = None,
    ) -> list[list[Chunk]]:
        """Per-sub-query ranked lists (NOT fused).

        QueryService's combined web-search path interleaves these with web
        searches concurrently, then fuses; exposing the un-fused lists
        lets it run one ``asyncio.gather`` over both.
        """
        return await asyncio.gather(
            *[
                self.retrieve(
                    partitions=partitions,
                    query=q,
                    top_k=top_k,
                    retrieval_top_k=retrieval_top_k,
                    filter_params=filter_params,
                    similarity_threshold=similarity_threshold,
                    disable_reranker=disable_reranker,
                    disable_expansion=disable_expansion,
                    resolved_plan=resolved_plan,
                )
                for q in queries
            ]
        )

    @staticmethod
    def fuse(
        doc_lists: list[list[Chunk]],
        top_k: int | None = None,
    ) -> list[Chunk]:
        """RRF-fuse ranked lists across partitions (and doc+web).

        Uses the canonical RRF constant (60) rather than a preset's ``rrf_k``:
        this fuses lists from *different* partitions (and the web branch), so no
        single partition's ``rrf_k`` applies. Per-partition ``rrf_k`` is honoured
        one layer down, in ``RetrieverPipeline.get_relevant_docs`` (#707).
        """
        fused = rrf_reranking(doc_lists, key_fn=_chunk_key)
        return fused[:top_k] if top_k is not None else fused


__all__ = ["ResolvedRetrievalGroup", "ResolvedRetrievalPlan", "RetrievalService"]
