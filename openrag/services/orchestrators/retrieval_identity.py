"""The public description of the retrieval settings, and its fingerprints.

A retrieval trace carries a fingerprint of the settings it ran with, so two
runs can be told apart: the endpoints (names and models only, never URLs or
keys), the retrieval and rerank settings, the expansion, the contextualizer and
its prompt. ``RetrievalIdentity`` builds that description from the partition
configs in ``Settings.partitions``; ``RetrievalService`` adds what only
resolving the pipelines tells (the reranker endpoint a partition actually
gets, the request's overrides).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from core.models.preset import resolve_partition_chat_llm
from core.prompts import load_template_by_key
from core.retrieval.trace import canonical_fingerprint

if TYPE_CHECKING:
    from core.config.root import Settings


def public_endpoint(config: Settings, endpoint_type: str, name: str | None) -> dict[str, object]:
    """Return endpoint identity only, excluding URLs, keys, and arbitrary extras."""
    registry = getattr(getattr(config, "models", None), endpoint_type, {}) or {}
    endpoint = registry.get(name) if name is not None else None
    return {
        "name": name,
        "model": getattr(endpoint, "model_name", None),
    }


def static_reranker_identity(config: Settings, *, exists: bool) -> dict[str, object]:
    """Describe the reranker built at startup from ``settings.reranker``; *exists* says whether it was."""
    if not exists:
        return {"name": None, "model": None}
    return {
        "name": "default",
        "model": getattr(getattr(config, "reranker", None), "model_name", None),
    }


def partition_search_identity(
    partition_name: str,
    configured_partitions: Mapping[str, Any],
    endpoints: Mapping[str, Any],
) -> tuple[str, Any | None, str | None, str]:
    """Resolve a partition to the embedder and vector field used by search.

    The last value groups the partitions searched together: the vector field,
    so the ``default`` alias and the embedder behind it share one search.
    """
    partition = configured_partitions.get(partition_name)
    embedder_name = partition.embedder if partition is not None else "default"
    endpoint = endpoints.get(embedder_name)
    vector_field = getattr(endpoint, "vector_field", None) if endpoint is not None else None
    group_key = vector_field or embedder_name
    return embedder_name, endpoint, vector_field, group_key


def public_prompt_identity(prompt: object) -> dict[str, str | None]:
    """The name, source and content hash of a resolved prompt, a mapping or an object."""

    def field(key: str) -> str | None:
        return prompt.get(key) if isinstance(prompt, Mapping) else getattr(prompt, key, None)

    return {
        "name": field("name"),
        "source": field("source"),
        "content_hash": field("content_hash"),
    }


class RetrievalIdentity:
    """Describes the retrieval settings a set of partitions is searched with.

    The ``catalog_*`` flags say whether the model-endpoint catalog resolves
    that kind of endpoint (always, once the container wires it); without it the
    static ``settings`` endpoints are described. *static_reranker* says whether
    the reranker built at startup from ``settings.reranker`` exists.
    *prompt_service* resolves the contextualizer prompt; without one, the disk
    seed is read.
    """

    def __init__(
        self,
        *,
        config: Settings,
        prompt_service: Any | None = None,
        catalog_llms: bool,
        catalog_rerankers: bool,
        catalog_embedders: bool,
        static_reranker: bool,
    ) -> None:
        # Live reference: the partition configs are refreshed on every preset change.
        self._config = config
        self._prompt_service = prompt_service
        self._catalog_llms = catalog_llms
        self._catalog_rerankers = catalog_rerankers
        self._catalog_embedders = catalog_embedders
        self._static_reranker = static_reranker

    def _partition_configs(self) -> dict[str, Any]:
        return getattr(self._config, "partitions", {}) or {}

    def _legacy_retriever_value(self, name: str, default: Any) -> Any:
        return getattr(self._config.retriever, name, default)

    def static_reranker_identity(self) -> dict[str, object]:
        """Describe the static reranker built at startup from ``settings.reranker``."""
        return static_reranker_identity(self._config, exists=self._static_reranker)

    def _effective_contextualizer_name(self, partitions: Sequence[str]) -> str:
        """Mirror QueryService's partition LLM resolution for public metadata."""
        if not self._catalog_llms:
            return "default"
        name = resolve_partition_chat_llm(list(partitions), self._partition_configs())
        registered = getattr(getattr(self._config, "models", None), "llm", {}) or {}
        return name if name is not None and name in registered else "default"

    def public_configuration(self, partitions: Sequence[str]) -> dict[str, object]:
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
            if not self._catalog_llms or (contextualizer_name == "default" and "default" not in llm_registry)
            else public_endpoint(self._config, "llm", contextualizer_name)
        )
        for partition_name in sorted(set(selected_partitions)):
            partition = configured_partitions.get(partition_name)
            retrieval = partition.retrieval if partition is not None else self._config.retriever
            embedder_name = partition.embedder if partition is not None else "default"
            reranker_enabled = bool(
                getattr(
                    retrieval,
                    "enable_reranker",
                    getattr(getattr(self._config, "reranker", None), "enabled", False),
                )
            )
            reranker_name = getattr(retrieval, "reranker", None) or ("default" if reranker_enabled else None)
            reranker_endpoint = (
                self.static_reranker_identity()
                if partition is None or not self._catalog_rerankers
                else public_endpoint(self._config, "reranker", reranker_name)
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
                    "embedder": public_endpoint(self._config, "embedder", embedder_name),
                    "retrieval": {
                        "top_k": getattr(retrieval, "top_k", None),
                        "similarity_threshold": getattr(retrieval, "similarity_threshold", None),
                        "rrf_k": getattr(retrieval, "rrf_k", 60),
                        "with_surrounding_chunks": self._legacy_retriever_value("with_surrounding_chunks", False),
                        "allow_filterless_fallback": self._legacy_retriever_value("allow_filterless_fallback", True),
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

    def fingerprint(self, partitions: Sequence[str]) -> str:
        """Fingerprint only allowlisted public settings for the authorized scope."""
        return canonical_fingerprint(self.public_configuration(partitions))

    async def resolved_configuration(
        self,
        partitions: Sequence[str],
        *,
        reranker_identities: Mapping[str, dict[str, object]],
        top_k: int | None,
        similarity_threshold: float | None,
        disable_reranker: bool,
        disable_expansion: bool,
        contextualizer_prompt: object | None,
        strict_prompt_resolution: bool,
    ) -> tuple[dict[str, object], str]:
        """The public settings as resolved for one request, and their fingerprint.

        *reranker_identities* are the reranker endpoints the partitions'
        pipelines resolved to; the overrides are the request's. The
        contextualizer prompt is described from *contextualizer_prompt* when
        the request already resolved it, else resolved here.
        """
        public = self.public_configuration(partitions)
        for public_partition in public["partitions"]:
            public_retrieval = public_partition["retrieval"]
            partition_name = public_partition["name"]
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
            public_prompt_identity(contextualizer_prompt)
            if contextualizer_prompt is not None
            else await self.contextualizer_prompt_identity(partitions, strict=strict_prompt_resolution)
        )
        return public, canonical_fingerprint(public)

    def public_search_configuration(
        self,
        partitions: Sequence[str],
        effective_options: Mapping[str, object],
        *,
        rerank: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Return only settings that can affect the raw search endpoint.

        *rerank* describes the reranker and the candidates a reranked search
        ran with.
        """
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
        if not self._catalog_embedders or not configured_partitions:
            public_partitions = [
                {"name": partition_name, "embedder": raw_search_embedder}
                for partition_name in sorted(set(selected_partitions))
            ]
        else:
            endpoints = getattr(getattr(self._config, "models", None), "embedder", {}) or {}
            public_partitions = []
            for partition_name in sorted(set(selected_partitions)):
                embedder_name, endpoint, vector_field, _group_key = partition_search_identity(
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
        if rerank is not None:
            configuration["rerank"] = rerank
        return configuration

    def _contextualizer_prompt_name(self, partitions: Sequence[str]) -> str | None:
        selected = list(dict.fromkeys(partitions))
        if len(selected) != 1 or "all" in selected:
            return None
        partition = self._partition_configs().get(selected[0])
        return getattr(getattr(partition, "retrieval", None), "query_contextualizer_prompt_name", None)

    async def contextualizer_prompt_identity(
        self,
        partitions: Sequence[str],
        *,
        strict: bool = False,
    ) -> dict[str, str | None]:
        """Name, source and content hash of the contextualizer prompt the partitions resolve."""
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
