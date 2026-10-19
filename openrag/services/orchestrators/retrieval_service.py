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
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.models.preset import resolve_partition_chat_llm
from core.models.retrieval_trace import TraceRemovalReason
from core.prompts import load_template_by_key
from core.retrieval.pipeline import RetrieverPipeline, _rerank_chunks, _safe_trace, _with_removal_reasons
from core.retrieval.retriever import (
    HyDeRetriever,
    MultiQueryRetriever,
    SingleRetriever,
    _expand_with_related_chunks,
)
from core.retrieval.rrf import rrf_reranking
from core.retrieval.searcher import file_id_restriction
from core.retrieval.trace import (
    RetrievalTraceBuilder,
    candidates_from_chunks,
    canonical_fingerprint,
    merge_child_traces,
    merge_query_traces,
)
from core.utils.consts import MAX_RERANK_CANDIDATES
from core.utils.exceptions import PartitionNotFoundError, ServiceUnavailableError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from core.models.chunk import Chunk
    from core.models.preset import PartitionConfig
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


@dataclass(frozen=True, slots=True)
class _PipelineSettings:
    """What a partition's retrieval pipeline is built from, after the caller's overrides.

    Partitions searched on one vector field with equal settings build the same
    pipeline, so they are retrieved together.
    """

    type: str
    expansion_llm: str | None
    expansion_prompt_name: str | None
    rerank: bool
    reranker: str | None
    top_k: int
    top_n: int
    similarity_threshold: float
    include_related: bool
    include_ancestors: bool
    rrf_k: int


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
        preset_service: Any | None = None,
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
        self._preset_service = preset_service
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
        """Pipeline built from the static ``settings.retriever``/``settings.reranker`` config.

        Used when the partition cache (``Settings.partitions``) is empty, or
        when an empty partition list is requested. A running service never
        gets there: startup seeds the ``default`` partition and loads the
        cache, and that partition cannot be deleted. An empty scope never
        reaches this service either: the search and chat routes answer 403,
        and the MCP search tool raises ``PermissionError("No accessible
        partitions")``, which the client sees as a tool error. Only unit tests
        that build the service without partitions, or a partition table
        emptied by hand in the database, still reach it.
        """
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
        # strategy (resolved from the preset's *_prompt_name in _pipeline_from_settings).
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
        *,
        strict: bool = False,
    ) -> tuple[str, dict[str, str | None]]:
        """Resolve a query-side prompt to its text and content-free identity.

        Prefers the library (named preset prompt -> type default) via
        PromptService; falls back to the on-disk seed when no PromptService is
        wired (unit tests / DB-less runs), so behaviour matches the pre-DB path.
        """
        resolve_with_identity = getattr(self._prompt_service, "resolve_prompt_with_identity", None)
        if resolve_with_identity is not None:
            resolved = await resolve_with_identity(prompt_type, names=[name], strict_errors=strict)
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

    async def refresh_partition_configs(self) -> None:
        """Reload partition settings another process changed, such as the embedder a swap switched."""
        if self._preset_service is not None:
            await self._preset_service.try_refresh_if_stale()

    def has_loaded_partition_retrieval_config(self, partition: str) -> bool:
        """Return whether this process has resolved retrieval settings for a partition."""
        return partition in self._partition_configs()

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

    def _legacy_reranker_identity(self) -> dict[str, object]:
        """Describe the startup reranker used by the legacy retrieval pipeline."""
        if self._legacy_reranker is None:
            return {"name": None, "model": None}
        return {
            "name": "default",
            "model": getattr(getattr(self._config, "reranker", None), "model_name", None),
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
            reranker_endpoint = (
                self._legacy_reranker_identity()
                if partition is None or self._reranker_factory is None
                else self._public_endpoint(self._config, "reranker", reranker_name)
            )
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
            # top_n as it applies: a preset that leaves it unset follows RERANKER_TOP_K.
            top_n = getattr(retrieval, "top_n", None)
            if top_n is None:
                top_n = getattr(getattr(self._config, "reranker", None), "top_k", None)
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
                        **reranker_endpoint,
                        "enabled": reranker_enabled,
                        "top_n": top_n,
                    },
                    "contextualizer": {
                        **contextualizer_endpoint,
                        "prompt_name": getattr(retrieval, "query_contextualizer_prompt_name", None),
                        "mode": getattr(getattr(self._config, "rag", None), "mode", None),
                        "max_contextualized_query_len": getattr(
                            getattr(self._config, "rag", None), "max_contextualized_query_len", None
                        ),
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
        if self._searcher_factory is None or not configured_partitions:
            public_partitions = [
                {"name": partition_name, "embedder": raw_search_embedder}
                for partition_name in sorted(set(selected_partitions))
            ]
        else:
            endpoints = getattr(getattr(self._config, "models", None), "embedder", {}) or {}
            public_partitions = []
            for partition_name in sorted(set(selected_partitions)):
                embedder_name, endpoint, vector_field, _group_key = self._partition_search_identity(
                    partition_name, configured_partitions, endpoints
                )
                public_partitions.append(
                    {
                        "name": partition_name,
                        "embedder": {
                            "name": embedder_name,
                            "model": getattr(endpoint, "model_name", None),
                            "vector_field": vector_field,
                        },
                    }
                )
        hybrid_enabled = getattr(getattr(self._config, "vectordb", None), "hybrid_search", None)
        configuration: dict[str, object] = {
            "operation": "raw_search",
            "hybrid": {
                "enabled": hybrid_enabled,
                "fusion": "rrf" if hybrid_enabled else None,
            },
            "partitions": public_partitions,
            "request": dict(effective_options),
        }
        if effective_options.get("rerank"):
            # What the rerank ran with, resolved as search() does: the request
            # holds rerank_candidates as sent (None when it follows the preset).
            rerank_partitions = list(partitions)
            _reranker, reranker_identity = self._search_reranker(rerank_partitions)
            configuration["rerank"] = {
                "reranker": reranker_identity,
                "candidates": self._search_rerank_candidates(
                    rerank_partitions, effective_options["top_k"], effective_options.get("rerank_candidates")
                ),
            }
        return configuration

    def search_configuration_fingerprint(
        self,
        partitions: Sequence[str],
        effective_options: Mapping[str, object],
    ) -> str:
        """Fingerprint the effective raw-search path without chat-only settings."""
        return canonical_fingerprint(self.public_search_configuration(partitions, effective_options))

    @staticmethod
    def _partition_search_identity(
        partition_name: str,
        configured_partitions: Mapping[str, Any],
        endpoints: Mapping[str, Any],
    ) -> tuple[str, Any | None, str | None, str]:
        """Resolve a partition to the embedder and vector field used by search."""
        partition = configured_partitions.get(partition_name)
        embedder_name = partition.embedder if partition is not None else "default"
        endpoint = endpoints.get(embedder_name)
        vector_field = getattr(endpoint, "vector_field", None) if endpoint is not None else None
        group_key = vector_field or embedder_name
        return embedder_name, endpoint, vector_field, group_key

    def _contextualizer_prompt_name(self, partitions: Sequence[str]) -> str | None:
        selected = list(dict.fromkeys(partitions))
        if len(selected) != 1 or "all" in selected:
            return None
        partition = self._partition_configs().get(selected[0])
        return getattr(getattr(partition, "retrieval", None), "query_contextualizer_prompt_name", None)

    async def _contextualizer_prompt_identity(
        self,
        partitions: Sequence[str],
        *,
        strict: bool = False,
    ) -> dict[str, str | None]:
        prompt_name = self._contextualizer_prompt_name(partitions)
        resolve_with_identity = getattr(self._prompt_service, "resolve_prompt_with_identity", None)
        if resolve_with_identity is not None:
            resolved = await resolve_with_identity(
                "query_contextualizer",
                names=[prompt_name],
                strict_errors=strict,
            )
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

    async def _query_expansion_prompt_identity(
        self,
        retrieval: object,
        *,
        strict: bool = False,
    ) -> dict[str, str | None] | None:
        retrieval_type = getattr(retrieval, "type", None)
        if retrieval_type == "hyde":
            prompt_type = "hyde"
            prompt_name = getattr(retrieval, "hyde_prompt_name", None)
        elif retrieval_type == "multiQuery":
            prompt_type = "multi_query"
            prompt_name = getattr(retrieval, "multi_query_prompt_name", None)
        else:
            return None
        _content, identity = await self._resolve_query_prompt(
            prompt_type,
            prompt_name,
            prompt_type,
            strict=strict,
        )
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

        1. The partition's configured ``reranker`` endpoint, resolved fresh via
           the model-endpoint catalog factory so a rename/promotion of that
           endpoint takes effect immediately.
        2. The **catalog default** endpoint (``is_default=True``) when the
           partition sets no reranker, or its endpoint name has gone stale (the
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
            return self._legacy_reranker, self._legacy_reranker_identity()
        if reranker_name:
            try:
                reranker = self._reranker_factory(reranker_name)
            except KeyError:
                logger.bind(reranker=reranker_name, partition=partition).warning(
                    "Partition reranker endpoint not found in the model-endpoint catalog — "
                    "falling back to the default reranker"
                )
            else:
                logger.bind(reranker=reranker_name, partition=partition).debug(
                    "Reranking with the partition's reranker endpoint"
                )
                return reranker, self._public_endpoint(self._config, "reranker", reranker_name)
        try:
            reranker = self._reranker_factory("default")
        except KeyError:
            pass
        else:
            logger.bind(reranker=self._default_reranker_name(), partition=partition).debug(
                "Reranking with the default reranker endpoint"
            )
            return reranker, self._public_endpoint(self._config, "reranker", "default")
        logger.bind(partition=partition).debug(
            "Reranking with the static default reranker (no catalog default endpoint)"
        )
        return self._legacy_reranker, self._legacy_reranker_identity()

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

    async def _pipeline_from_settings(
        self,
        partition: str,
        embedder: str,
        settings: _PipelineSettings,
    ) -> tuple[RetrieverPipeline, int | None, dict[str, str | None] | None, dict[str, object] | None]:
        """The pipeline *settings* describe, searching *embedder*'s vectors.

        It reads no partition config, so a group's pipeline is the one it was
        grouped by, even when a config refresh lands while an earlier group
        resolves its prompt. *partition* only names the group in the logs.
        """
        searcher = self._searcher_factory(embedder) if self._searcher_factory is not None else self._searcher

        llm = self._legacy_llm
        if settings.expansion_llm is not None and self._llm_factory is not None:
            llm = self._llm_factory(settings.expansion_llm)

        # Only the expansion strategies need a prompt; type="single" (the common
        # case) resolves nothing, so the DB is never touched on that path.
        template = None
        prompt_identity = None
        if settings.type == "multiQuery":
            template, prompt_identity = await self._resolve_query_prompt(
                "multi_query", settings.expansion_prompt_name, "multi_query"
            )
            prompt_identity = {"type": "multi_query", **prompt_identity}
        elif settings.type == "hyde":
            template, prompt_identity = await self._resolve_query_prompt("hyde", settings.expansion_prompt_name, "hyde")
            prompt_identity = {"type": "hyde", **prompt_identity}

        reranker = None
        reranker_identity = None
        if settings.rerank:
            reranker, reranker_identity = self._resolve_reranker_with_identity(settings.reranker, partition)

        retriever = self._build_retriever(
            rtype=settings.type,
            template=template,
            common={
                "searcher": searcher,
                "top_k": settings.top_k,
                "similarity_threshold": settings.similarity_threshold,
                "with_surrounding_chunks": self._legacy_retriever_value("with_surrounding_chunks", False),
                "include_related": settings.include_related,
                "include_ancestors": settings.include_ancestors,
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
            reranker_top_k=settings.top_n,
            allow_filterless_fallback=self._legacy_retriever_value("allow_filterless_fallback", True),
            rrf_k=settings.rrf_k,
        )
        return pipeline, settings.top_n, prompt_identity, reranker_identity

    def _pipeline_settings(
        self,
        partition_cfg: PartitionConfig,
        *,
        top_k: int | None,
        similarity_threshold: float | None,
        disable_reranker: bool,
        disable_expansion: bool,
    ) -> _PipelineSettings:
        """The settings *partition_cfg*'s pipeline is built from, after the caller's overrides.

        Only what the pipeline reads: a preset field it ignores (the reranker
        when reranking is off, the query-rewriting LLM and prompts of a
        ``single`` preset, the contextualizer prompt, which QueryService reads,
        a value the caller overrides) cannot split partitions into separate
        retrievals.
        """
        cfg = partition_cfg.retrieval
        rerank = cfg.enable_reranker and not disable_reranker
        expansion_prompt_names = {"multiQuery": cfg.multi_query_prompt_name, "hyde": cfg.hyde_prompt_name}
        expands = cfg.type in expansion_prompt_names
        return _PipelineSettings(
            type=cfg.type,
            # multiQuery and hyde rewrite the query with the preset's LLM, else the partition's chat LLM.
            expansion_llm=(cfg.llm or partition_cfg.chat_llm or "default") if expands else None,
            expansion_prompt_name=expansion_prompt_names.get(cfg.type),
            rerank=rerank,
            reranker=cfg.reranker if rerank else None,
            top_k=top_k if top_k is not None else cfg.top_k,
            top_n=cfg.effective_top_n(self._config.reranker.top_k),
            similarity_threshold=similarity_threshold if similarity_threshold is not None else cfg.similarity_threshold,
            include_related=cfg.include_related and not disable_expansion,
            include_ancestors=cfg.include_ancestors and not disable_expansion,
            rrf_k=cfg.rrf_k,
        )

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
        # Callers only ever pass a concrete partition name. With no per-partition
        # configs at all, fall back to the legacy pipeline.
        if not self._partition_configs():
            return self._pipeline, None
        partition_cfg = self._require_partition_config(partition)
        settings = self._pipeline_settings(
            partition_cfg,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            disable_reranker=disable_reranker,
            disable_expansion=disable_expansion,
        )
        pipeline, default_top_k, _prompt_identity, _reranker_identity = await self._pipeline_from_settings(
            partition, partition_cfg.embedder, settings
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
        strict_prompt_resolution: bool = False,
    ) -> ResolvedRetrievalPlan:
        """Resolve executable pipelines and their public identity once."""
        await self.refresh_partition_configs()
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
                    await self._query_expansion_prompt_identity(retrieval, strict=strict_prompt_resolution)
                    if configs
                    else self._legacy_query_expansion_prompt_identity(self._pipeline)
                )
                reranker_enabled = getattr(
                    retrieval,
                    "enable_reranker",
                    getattr(getattr(self._config, "reranker", None), "enabled", False),
                )
                if partition is not None and reranker_enabled and not disable_reranker:
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
            partition_groups = self._partitions_by_pipeline(
                selected,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
            )
            for names, embedder, settings in partition_groups:
                # Every partition of a group shares the pipeline, prompt and reranker it was grouped by.
                (
                    pipeline,
                    default_top_k,
                    prompt_identity,
                    reranker_identity,
                ) = await self._pipeline_from_settings(names[0], embedder, settings)
                groups.append(ResolvedRetrievalGroup(tuple(names), pipeline, default_top_k))
                for partition_name in names:
                    prompt_identities[partition_name] = prompt_identity
                    if reranker_identity is not None:
                        reranker_identities[partition_name] = reranker_identity
            if len(groups) == 1:
                # Keep the requested scope, so "all" stays unscoped, as in _pipeline_groups_for_partitions.
                (group,) = groups
                groups = [
                    ResolvedRetrievalGroup(
                        tuple(["all"] if "all" in requested else selected), group.pipeline, group.default_top_k
                    )
                ]

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
            else await self._contextualizer_prompt_identity(requested, strict=strict_prompt_resolution)
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
        """The partitions grouped by the pipeline that retrieves them, each with that pipeline.

        See :meth:`_partitions_by_pipeline` for the grouping. The groups' lists
        are fused with RRF by the caller.

        ``"all"`` only reaches this layer post-authorization (a SUPER_ADMIN_MODE
        admin; regular users are already expanded to their memberships upstream),
        so every hydrated partition is in scope. With a single group it is kept,
        so the search stays unscoped, as in :meth:`_search_groups`.
        """
        await self.refresh_partition_configs()
        configs = self._partition_configs()
        if not partitions or not configs:
            # Nothing to expand (no partitions exist yet) — keep the single
            # legacy pipeline; there is no per-partition config to honour.
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
        partition_groups = self._partitions_by_pipeline(
            list(configs) if "all" in partitions else partitions,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            disable_reranker=disable_reranker,
            disable_expansion=disable_expansion,
        )
        for names, embedder, settings in partition_groups:
            # Every partition of a group shares the pipeline it was grouped by.
            pipeline, default_top_k, _prompt_identity, _reranker_identity = await self._pipeline_from_settings(
                names[0], embedder, settings
            )
            groups.append((names, pipeline, default_top_k))
        if len(groups) == 1:
            # Keep the caller's partitions, so "all" stays unscoped.
            _, pipeline, default_top_k = groups[0]
            return [(partitions, pipeline, default_top_k)]
        return groups

    def _partitions_by_pipeline(
        self,
        partitions: Sequence[str],
        *,
        top_k: int | None,
        similarity_threshold: float | None,
        disable_reranker: bool,
        disable_expansion: bool,
    ) -> list[tuple[list[str], str, _PipelineSettings]]:
        """Concrete partitions grouped by the retrieval pipeline they build, each with its embedder and settings.

        A query embedding only matches vectors of the same model, so partitions
        on different embedders need their own retrieval. Partitions on the same
        vector field (so the ``default`` alias and the embedder behind it stay
        together, as in :meth:`_search_groups`) with the same pipeline settings
        (:meth:`_pipeline_settings`: the preset fields the pipeline reads, after
        the caller's overrides) build the same pipeline, so one retrieval serves
        them all: one query embedding, one search over all of them, one rerank
        pass, and their chunks compete on score rather than being fused by rank.
        """
        configs = self._partition_configs()
        endpoints = getattr(self._config.models, "embedder", None) or {}
        grouped: dict[tuple[str, _PipelineSettings], tuple[str, list[str]]] = {}
        for partition in partitions:
            partition_cfg = self._require_partition_config(partition)
            embedder, _endpoint, _vector_field, search_key = self._partition_search_identity(
                partition, configs, endpoints
            )
            settings = self._pipeline_settings(
                partition_cfg,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
            )
            # A group's pipeline searches with its first partition's embedder, as in _search_groups.
            grouped.setdefault((search_key, settings), (embedder, []))[1].append(partition)
        return [(names, embedder, settings) for (_key, settings), (embedder, names) in grouped.items()]

    def _search_groups(self, partitions: list[str]) -> list[tuple[list[str], RetrievalSearcher]]:
        """The partitions grouped by the vector field they are searched on, each with its searcher.

        A query embedding only matches vectors of the same model, so each
        embedder's partitions need their own search. Grouping by field rather
        than embedder name keeps the ``default`` alias and the embedder behind
        it in one search. A partition without a config is searched with the
        default embedder, as before.
        """
        configs = self._partition_configs()
        if self._searcher_factory is None or not configs or not partitions:
            return [(partitions, self._searcher)]
        expanded = list(configs) if "all" in partitions else partitions
        endpoints = getattr(self._config.models, "embedder", None) or {}
        groups: dict[str, tuple[str, list[str]]] = {}
        for partition in expanded:
            embedder, _endpoint, _vector_field, group_key = self._partition_search_identity(
                partition, configs, endpoints
            )
            groups.setdefault(group_key, (embedder, []))[1].append(partition)
        if len(groups) == 1:
            # Keep the caller's partitions, so "all" stays unscoped.
            ((embedder, _),) = groups.values()
            return [(partitions, self._searcher_factory(embedder))]
        return [(names, self._searcher_factory(embedder)) for embedder, names in groups.values()]

    def _single_partition_retrieval_config(self, partitions: list[str]):
        """The retrieval preset of a search scoped to exactly one configured partition, else ``None``."""
        if len(partitions) != 1:
            return None
        partition_cfg = self._partition_configs().get(partitions[0])
        return partition_cfg.retrieval if partition_cfg is not None else None

    def _search_reranker(self, partitions: list[str]) -> tuple[Reranker, dict[str, object]]:
        """Reranker for an explicit ``search(rerank=True)``, with its public identity.

        A single-partition search uses that partition's reranker preset, same
        resolution as the chat pipeline. A search spanning several partitions is
        reranked as one list, so it uses the default reranker: scores from
        different models are not comparable.

        Availability follows ``reranker.enabled``, not the catalog: the seeder
        registers the reranker endpoint even when reranking is off (a Helm
        install with ``reranker.enabled: false`` still points
        ``RERANKER_BASE_URL`` at a Service that doesn't exist), so resolving a
        reranker proves nothing. A disabled reranker is reachable only through
        the per-partition opt-in the seeder describes: a single-partition search
        whose retrieval preset sets ``enable_reranker``. The preset can only
        switch reranking on, never off — with the deployment's reranker enabled,
        an explicit ``rerank=True`` still reranks a partition whose preset
        disables it for chat.
        """
        pipeline_cfg = self._single_partition_retrieval_config(partitions)
        opted_in = pipeline_cfg is not None and pipeline_cfg.enable_reranker
        if not self._config.reranker.enabled and not opted_in:
            raise ServiceUnavailableError(
                "Reranking was requested but the reranker is disabled on this deployment; "
                "only a search over one partition whose retrieval preset enables it can rerank.",
                code="RERANKER_UNAVAILABLE",
            )
        reranker, identity = self._resolve_reranker_with_identity(
            pipeline_cfg.reranker if pipeline_cfg else None, ",".join(partitions)
        )
        if reranker is None:
            raise ServiceUnavailableError(
                "Reranking was requested but no reranker is configured on this deployment.",
                code="RERANKER_UNAVAILABLE",
            )
        return reranker, identity

    def _search_rerank_candidates(self, partitions: list[str], top_k: int, requested: int | None) -> int:
        """Number of vector candidates handed to the reranker by ``search``.

        ``requested`` defaults to the partition preset's ``top_k``
        (``retriever.top_k`` over several partitions), is never below
        ``top_k`` and is clamped to ``MAX_RERANK_CANDIDATES``.
        """
        pipeline_cfg = self._single_partition_retrieval_config(partitions)
        default = pipeline_cfg.top_k if pipeline_cfg else self._config.retriever.top_k
        return min(max(top_k, requested or default), MAX_RERANK_CANDIDATES)

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
        rerank: bool = False,
        rerank_candidates: int | None = None,
        trace: RetrievalTraceBuilder | None = None,
    ) -> list[Chunk]:
        """One similarity search per embedder, then optional reranking and related/ancestor expansion.

        Without ``rerank`` this is a faithful port of ``indexer.asearch`` + the
        legacy ``_expand_with_related_chunks`` (no query generation / reranking —
        those belong to QueryService). Partitions on different embedders are
        searched separately, each with its own model's query embedding; their
        hits are fused with RRF and cut to ``top_k`` before the surrounding
        chunks are added.

        With ``rerank`` each search fetches ``rerank_candidates`` chunks instead
        of ``top_k``, the reranker reorders them, and only the best ``top_k``
        are kept (as ``ScoredChunk``s carrying ``rerank_score``). Surrounding
        chunks are not fetched on that path, so the reranker scores exactly the
        candidates asked for and ``top_k`` is an exact cap. The candidate count
        is clamped to ``MAX_RERANK_CANDIDATES``, so a ``top_k`` above it returns
        at most that many reranked chunks. Related and ancestor chunks are still
        appended after the cut, unscored, so they can take the result past
        ``top_k``.
        """
        await self.refresh_partition_configs()
        parts = [partitions] if isinstance(partitions, str) else list(partitions)
        reranker = None
        candidates = top_k
        if rerank:
            reranker, _identity = self._search_reranker(parts)
            candidates = self._search_rerank_candidates(parts, top_k, rerank_candidates)
        if trace is not None:
            trace.record_stage("original_query", status="complete", candidates=[])
        groups = self._search_groups(parts)
        search_kwargs = {
            "query": text,
            "top_k": candidates,
            "filter": filter,
            "filter_params": filter_params,
            "similarity_threshold": similarity_threshold,
        }
        searcher = groups[0][1]
        if len(groups) == 1:
            trace_kwargs = {"trace": trace} if trace is not None else {}
            chunks = await searcher.search(
                partition=groups[0][0], with_surrounding_chunks=reranker is None, **search_kwargs, **trace_kwargs
            )
        else:
            group_traces = (
                [
                    RetrievalTraceBuilder(
                        request_id=f"{trace.request_id}:partition:{index}",
                        original_query=text,
                        partition=",".join(names),
                        diagnostics=trace.diagnostics,
                    )
                    for index, (names, _) in enumerate(groups)
                ]
                if trace is not None
                else []
            )
            try:
                group_legs = await self._gather_partition_groups(
                    [
                        (
                            names,
                            group_searcher.search(
                                partition=names,
                                with_surrounding_chunks=False,
                                **search_kwargs,
                                **({"trace": group_traces[index]} if trace is not None else {}),
                            ),
                        )
                        for index, (names, group_searcher) in enumerate(groups)
                    ],
                    child_traces=group_traces if trace is not None else None,
                    error_stage="partition_search",
                )
                group_hits = [hits for _names, hits in group_legs]
            finally:
                if trace is not None:
                    try:
                        merge_query_traces(trace, group_traces)
                    except Exception as error:
                        try:
                            trace.record_error("partition_traces", error)
                        except Exception:
                            pass
            if trace is None:
                hits = self.fuse(group_hits, top_k=candidates)
            else:
                fused_hits = self.fuse(group_hits)
                hits = fused_hits[:candidates]
                selected_ids = {_chunk_key(chunk) for chunk in hits}
                trace_candidates = candidates_from_chunks(
                    fused_hits,
                    limit=trace.candidate_capacity_for_stage("partition_fused"),
                )
                trace_candidates = [
                    candidate
                    if candidate.id in selected_ids
                    else candidate.model_copy(
                        update={
                            "removal_reason": TraceRemovalReason(
                                code="partition_top_k",
                                explanation="Candidate fell outside the cross-embedder top-k "
                                "(rerank_candidates when reranking).",
                            )
                        }
                    )
                    for candidate in trace_candidates
                ]
                trace.record_stage(
                    "partition_fused",
                    status="complete",
                    candidates=trace_candidates,
                    candidate_count=len(fused_hits),
                )
            chunks = hits
            if reranker is None:
                chunks = await self._with_surrounding_chunks(searcher, hits, parts, filter_params)
        if reranker is not None:
            chunks = await self._rerank_search_hits(reranker, text, chunks, top_k, trace)
        if include_related or include_ancestors:
            chunks = await _expand_with_related_chunks(
                searcher=searcher,
                results=chunks,
                include_related=include_related,
                include_ancestors=include_ancestors,
                related_limit=related_limit,
                max_ancestor_depth=max_ancestor_depth,
                filter_params=filter_params,
                filter=filter,
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

    async def _rerank_search_hits(
        self,
        reranker: Reranker,
        text: str,
        hits: list[Chunk],
        top_k: int,
        trace: RetrievalTraceBuilder | None,
    ) -> list[Chunk]:
        """The best ``top_k`` of ``hits`` by the reranker, as ``ScoredChunk``s.

        Traced the way ``RetrieverPipeline.retrieve_docs`` traces its rerank:
        ``pre_rerank`` holds the candidates sent to the reranker, the ones cut
        by ``top_k`` marked ``reranker_top_n``, and ``post_rerank`` the chunks
        kept, with the reranker's duration. A tracing failure is recorded on
        the trace and never fails the search.
        """
        started = time.perf_counter()
        kept = (await _rerank_chunks(reranker, text, hits))[:top_k]
        elapsed = time.perf_counter() - started
        if trace is None:
            return kept
        kept_ids = {str(_chunk_key(chunk)) for chunk in kept}
        _safe_trace(
            trace,
            "pre_rerank",
            lambda: trace.record_stage(
                "pre_rerank",
                status="complete",
                candidates=_with_removal_reasons(
                    trace.project_chunks("pre_rerank", hits),
                    {str(_chunk_key(chunk)) for chunk in hits} - kept_ids,
                    "reranker_top_n",
                    "Fell outside the reranked top_k.",
                ),
                candidate_count=len(hits),
            ),
        )
        _safe_trace(
            trace,
            "post_rerank",
            lambda: trace.record_stage(
                "post_rerank",
                status="complete",
                candidates=trace.project_chunks("post_rerank", kept),
                candidate_count=len(kept),
                duration_seconds=elapsed,
            ),
        )
        _safe_trace(
            trace,
            "reranking",
            lambda: trace.timings.__setitem__("reranking", trace.timings.get("reranking", 0.0) + elapsed),
        )
        return kept

    async def _with_surrounding_chunks(
        self, searcher: RetrievalSearcher, hits: list[Chunk], partitions: list[str], filter_params: dict | None
    ) -> list[Chunk]:
        """``hits`` followed by their neighbouring chunks, deduplicated by id."""
        # Neighbouring chunks are read by section id, whatever the embedder.
        # They are context only: a failed lookup must not discard the hits
        # the search already kept.
        try:
            surrounding = await searcher.get_surrounding_chunks(
                chunks=hits, allowed_file_ids=file_id_restriction(filter_params)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(partitions=partitions).warning(
                f"Retrieval degraded: surrounding chunks skipped — {type(exc).__name__}: {exc}"
            )
            surrounding = []
        seen = {c.id for c in hits}
        return hits + [c for c in surrounding if c.id not in seen]

    # ------------------------------------------------------------------
    # Pipeline retrieval (powers QueryService — 8C.2)
    # ------------------------------------------------------------------

    async def _gather_partition_groups(
        self,
        legs: list[tuple[list[str], Awaitable[list]]],
        *,
        child_traces: Sequence[RetrievalTraceBuilder] | None = None,
        error_stage: str = "partition_search",
    ) -> list[tuple[list[str], list]]:
        """Await one coroutine per partition group, bounding concurrency.

        Each leg is ``(partition_names, coroutine)``; the names are carried so a
        dropped leg can be named in the log. Returns ``(partition_names, result)``
        for each leg that did not fail.

        Small fan-outs (the common case: a handful of groups) run fully
        parallel via a plain gather — no added overhead, byte-identical to the
        prior behaviour. Only a fan-out larger than ``max_partition_concurrency``
        (e.g. a SUPER_ADMIN_MODE ``openrag-all`` over partitions on many
        embedders or retrieval presets) is throttled through a per-call
        semaphore, so one request cannot launch a group-count-proportional
        flood of embed+Milvus calls (#708).

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

        survivors: list[tuple[list[str], list]] = []
        first_error: BaseException | None = None
        for index, ((partition_names, _), result) in enumerate(zip(legs, results, strict=True)):
            if not isinstance(result, BaseException):
                survivors.append((partition_names, result))
                continue
            if isinstance(result, asyncio.CancelledError):
                raise result
            if child_traces is not None and isinstance(result, Exception):
                try:
                    child_traces[index].record_error(error_stage, result)
                except Exception:
                    pass
            if first_error is None:
                first_error = result
            logger.bind(partitions=partition_names).warning(
                f"Retrieval degraded: dropping partition(s) {partition_names} — {type(result).__name__}: {result}"
            )

        if first_error is not None and not survivors:
            raise first_error
        return survivors

    @staticmethod
    def _record_degraded_final(
        trace: RetrievalTraceBuilder | None,
        chunks: list[Chunk],
    ) -> None:
        if trace is None:
            return
        try:
            trace.record_stage(
                "final",
                status="complete",
                candidates=trace.project_chunks("final", chunks),
                candidate_count=len(chunks),
            )
        except Exception as error:
            try:
                trace.record_error("final", error)
            except Exception:
                pass

    @staticmethod
    def _safe_merge_child_traces(
        trace: RetrievalTraceBuilder,
        children: Sequence[RetrievalTraceBuilder],
    ) -> None:
        try:
            merge_child_traces(trace, children)
        except Exception as error:
            try:
                trace.record_error("partition_traces", error)
            except Exception:
                pass

    async def retrieve(
        self,
        *,
        partitions: list[str],
        query: Query,
        top_k: int | None = None,
        retrieval_top_k: int | None = None,
        filter_params: dict | None = None,
        trace: RetrievalTraceBuilder | None = None,
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
        child_traces = (
            [
                RetrievalTraceBuilder(
                    f"{trace.request_id}:partition:{index}",
                    query.query,
                    partition=partition_group[0] if len(partition_group) == 1 else None,
                    diagnostics=trace.diagnostics,
                )
                for index, (partition_group, _pipeline, _default_top_k) in enumerate(groups)
            ]
            if trace is not None and len(groups) > 1
            else None
        )
        try:
            ranked_groups = await self._gather_partition_groups(
                [
                    (
                        partition_group,
                        pipeline.retrieve_docs(
                            partition=partition_group,
                            query=query,
                            top_k=top_k if top_k is not None else default_top_k,
                            filter_params=filter_params,
                            trace=child_traces[index] if child_traces is not None else trace,
                        ),
                    )
                    for index, (partition_group, pipeline, default_top_k) in enumerate(groups)
                ],
                child_traces=child_traces,
                error_stage="partition_retrieval",
            )
        finally:
            if trace is not None and child_traces is not None:
                self._safe_merge_child_traces(trace, child_traces)
        if len(ranked_groups) == 1:
            ((_names, result),) = ranked_groups
            if len(groups) > 1:
                self._record_degraded_final(trace, result)
            return result
        return self.fuse(
            [chunks for _names, chunks in ranked_groups],
            top_k=top_k,
            trace=trace,
            partition_counts=[len(names) for names, _chunks in ranked_groups],
        )

    async def retrieve_multi(
        self,
        *,
        partitions: list[str],
        search_queries: SearchQueries,
        top_k: int | None = None,
        retrieval_top_k: int | None = None,
        filter_params: dict | None = None,
        trace: RetrievalTraceBuilder | None = None,
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
        child_traces = (
            [
                RetrievalTraceBuilder(
                    f"{trace.request_id}:partition:{index}",
                    search_queries.query_list[0].query if len(search_queries.query_list) == 1 else None,
                    partition=partition_group[0] if len(partition_group) == 1 else None,
                    diagnostics=trace.diagnostics,
                )
                for index, (partition_group, _pipeline, _default_top_k) in enumerate(groups)
            ]
            if trace is not None and len(groups) > 1
            else None
        )
        try:
            ranked_groups = await self._gather_partition_groups(
                [
                    (
                        partition_group,
                        pipeline.get_relevant_docs(
                            partition=partition_group,
                            search_queries=search_queries,
                            top_k=top_k if top_k is not None else default_top_k,
                            filter_params=filter_params,
                            trace=child_traces[index] if child_traces is not None else trace,
                        ),
                    )
                    for index, (partition_group, pipeline, default_top_k) in enumerate(groups)
                ],
                child_traces=child_traces,
                error_stage="partition_retrieval",
            )
        finally:
            if trace is not None and child_traces is not None:
                self._safe_merge_child_traces(trace, child_traces)
        if len(ranked_groups) == 1:
            ((_names, result),) = ranked_groups
            if len(groups) > 1:
                self._record_degraded_final(trace, result)
            return result
        return self.fuse(
            [chunks for _names, chunks in ranked_groups],
            top_k=top_k,
            trace=trace,
            partition_counts=[len(names) for names, _chunks in ranked_groups],
        )

    async def retrieve_per_query(
        self,
        *,
        partitions: list[str],
        queries: list[Query],
        top_k: int | None = None,
        retrieval_top_k: int | None = None,
        filter_params: dict | None = None,
        trace: RetrievalTraceBuilder | None = None,
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
        child_traces = (
            [
                RetrievalTraceBuilder(
                    f"{trace.request_id}:query:{index}",
                    query.query,
                    diagnostics=trace.diagnostics,
                )
                for index, query in enumerate(queries)
            ]
            if trace is not None and len(queries) > 1
            else None
        )
        tasks = [
            self.retrieve(
                partitions=partitions,
                query=query,
                top_k=top_k,
                retrieval_top_k=retrieval_top_k,
                filter_params=filter_params,
                trace=child_traces[index] if child_traces is not None else trace,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
                resolved_plan=resolved_plan,
            )
            for index, query in enumerate(queries)
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=child_traces is not None)
            if child_traces is not None:
                for child, result in zip(child_traces, results, strict=True):
                    if isinstance(result, Exception):
                        try:
                            child.record_error("query_retrieval", result)
                        except Exception:
                            pass
        finally:
            if trace is not None and child_traces is not None:
                self._safe_merge_child_traces(trace, child_traces)
        if child_traces is not None:
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    raise result
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        return results

    @staticmethod
    def fuse(
        doc_lists: list[list[Chunk]],
        top_k: int | None = None,
        trace: RetrievalTraceBuilder | None = None,
        partition_counts: Sequence[int] | None = None,
    ) -> list[Chunk]:
        """RRF-fuse ranked lists across partition groups (and doc+web).

        Uses the canonical RRF constant (60) rather than a preset's ``rrf_k``:
        this fuses lists from *different* partition groups (and the web branch),
        so no single preset's ``rrf_k`` applies. A preset's ``rrf_k`` is honoured
        one layer down, in ``RetrieverPipeline.get_relevant_docs`` (#707).

        ``partition_counts`` gives how many partitions each list ranks together.
        A list over ``n`` partitions has its ranks divided by ``n``, so it takes
        about ``n`` times the share of the fused top a one-partition list takes:
        each partition keeps roughly the share it had as a list of its own, and
        the partitions of a group split theirs by score.
        """
        fused = rrf_reranking(
            doc_lists,
            key_fn=_chunk_key,
            top_k=top_k,
            trace=trace,
            trace_stage="partition_fused",
            rank_scales=partition_counts,
        )
        if trace is not None:
            try:
                trace.record_stage(
                    "final",
                    status="complete",
                    candidates=trace.project_chunks("final", fused),
                    candidate_count=len(fused),
                )
            except Exception as error:
                try:
                    trace.record_error("final", error)
                except Exception:
                    pass
        return fused


__all__ = ["ResolvedRetrievalGroup", "ResolvedRetrievalPlan", "RetrievalService"]
