"""Builds the retrieval pipelines, and the ``RetrievalService`` that fans queries out over them.

``RetrievalPipelineFactory`` implements ``core.retrieval.pipeline_factory.PipelineFactory``:
from the partition configs cached in ``Settings.partitions`` it decides which
partitions are retrieved together, with which searcher (the partition's own
embedder), reranker and pipeline settings. A partition missing from the config
cache is a ``PartitionNotFoundError``: there is no fallback pipeline built from
the static ``settings.retriever`` config.

Pipelines are built per request, not cached: a pipeline is two small objects,
and the searchers' embedders and the rerankers come from the container's
endpoint factories, which cache their clients. A preset or endpoint change
therefore applies to the next request with nothing to invalidate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.retrieval.pipeline import RetrieverPipeline
from core.retrieval.pipeline_factory import PipelineGroup
from core.retrieval.retriever import SingleRetriever
from core.utils.exceptions import PartitionNotFoundError
from core.utils.logging import get_logger
from services.orchestrators.retrieval_identity import (
    RetrievalIdentity,
    partition_search_identity,
    public_endpoint,
    static_reranker_identity,
)
from services.orchestrators.retrieval_service import RetrievalService

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM
    from core.models.preset import PartitionConfig
    from core.rerankers.reranker import Reranker
    from core.retrieval.searcher import RetrievalSearcher

logger = get_logger()


@dataclass(frozen=True, slots=True)
class _PipelineSettings:
    """What a partition's retrieval pipeline is built from, after the caller's overrides.

    Partitions searched on one vector field with equal settings build the same
    pipeline, so they are retrieved together.
    """

    rerank: bool
    reranker: str | None
    top_k: int
    top_n: int
    similarity_threshold: float
    include_related: bool
    include_ancestors: bool
    rrf_k: int


class RetrievalPipelineFactory:
    """The searchers, rerankers and pipelines that serve each partition.

    *searcher* and *reranker* are the defaults built at startup from the static
    ``settings``; *searcher_factory* and *reranker_factory* resolve a named
    model endpoint from the catalog. Without a factory (unit tests) the default
    is used for every partition.
    """

    def __init__(
        self,
        *,
        config: Settings,
        searcher: RetrievalSearcher,
        reranker: Reranker | None = None,
        searcher_factory: Callable[[str], RetrievalSearcher] | None = None,
        reranker_factory: Callable[[str], Reranker] | None = None,
    ) -> None:
        # Live reference: the partition configs are refreshed on every preset change.
        self._config = config
        self._searcher = searcher
        self._static_reranker = reranker
        self._searcher_factory = searcher_factory
        self._reranker_factory = reranker_factory

    def _partition_configs(self) -> dict[str, Any]:
        return getattr(self._config, "partitions", {}) or {}

    def _require_partition_config(self, partition: str) -> PartitionConfig:
        partitions = self._partition_configs()
        if partition not in partitions:
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
        return partitions[partition]

    def _legacy_retriever_value(self, name: str, default: Any) -> Any:
        return getattr(self._config.retriever, name, default)

    def _static_reranker_with_identity(self) -> tuple[Reranker | None, dict[str, object]]:
        return self._static_reranker, static_reranker_identity(self._config, exists=self._static_reranker is not None)

    def reranker(self, name: str | None, partition: str) -> tuple[Reranker | None, dict[str, object]]:
        """Effective reranker for one partition's retrieval pipeline.

        Resolution order — mirrors ``ChatService._resolve_llm``:

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
            return self._static_reranker_with_identity()
        if name:
            try:
                reranker = self._reranker_factory(name)
            except KeyError:
                logger.bind(reranker=name, partition=partition).warning(
                    "Partition reranker endpoint not found in the model-endpoint catalog — "
                    "falling back to the default reranker"
                )
            else:
                logger.bind(reranker=name, partition=partition).debug(
                    "Reranking with the partition's reranker endpoint"
                )
                return reranker, public_endpoint(self._config, "reranker", name)
        try:
            reranker = self._reranker_factory("default")
        except KeyError:
            pass
        else:
            logger.bind(reranker=self._default_reranker_name(), partition=partition).debug(
                "Reranking with the default reranker endpoint"
            )
            return reranker, public_endpoint(self._config, "reranker", "default")
        logger.bind(partition=partition).debug(
            "Reranking with the static default reranker (no catalog default endpoint)"
        )
        return self._static_reranker_with_identity()

    def _default_reranker_name(self) -> str:
        """Real endpoint name behind the catalog reranker ``"default"`` alias, for logging.

        Same identity-lookup trick as ``ChatService._default_llm_name``: the
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

    def _searcher_for(self, embedder: str) -> RetrievalSearcher:
        return self._searcher_factory(embedder) if self._searcher_factory is not None else self._searcher

    def search_groups(self, partitions: list[str]) -> list[tuple[list[str], RetrievalSearcher]]:
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
            embedder, _endpoint, _vector_field, group_key = partition_search_identity(partition, configs, endpoints)
            groups.setdefault(group_key, (embedder, []))[1].append(partition)
        if len(groups) == 1:
            # Keep the caller's partitions, so "all" stays unscoped.
            ((embedder, _),) = groups.values()
            return [(partitions, self._searcher_factory(embedder))]
        return [(names, self._searcher_factory(embedder)) for embedder, names in groups.values()]

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
        when reranking is off, the contextualizer prompt, which ChatService
        reads, a value the caller overrides) cannot split partitions into
        separate retrievals.
        """
        cfg = partition_cfg.retrieval
        rerank = cfg.enable_reranker and not disable_reranker
        return _PipelineSettings(
            rerank=rerank,
            reranker=cfg.reranker if rerank else None,
            top_k=top_k if top_k is not None else cfg.top_k,
            top_n=cfg.effective_top_n(self._config.reranker.top_k),
            similarity_threshold=similarity_threshold if similarity_threshold is not None else cfg.similarity_threshold,
            include_related=cfg.include_related and not disable_expansion,
            include_ancestors=cfg.include_ancestors and not disable_expansion,
            rrf_k=cfg.rrf_k,
        )

    def _pipeline(self, partitions: list[str], embedder: str, settings: _PipelineSettings) -> PipelineGroup:
        """The pipeline *settings* describe, searching *embedder*'s vectors, for *partitions*.

        It reads no partition config, so a group's pipeline is the one it was
        grouped by.
        """
        reranker = None
        reranker_identity = None
        if settings.rerank:
            # The first partition only names the group in the logs.
            reranker, reranker_identity = self.reranker(settings.reranker, partitions[0])

        retriever = SingleRetriever(
            searcher=self._searcher_for(embedder),
            top_k=settings.top_k,
            similarity_threshold=settings.similarity_threshold,
            with_surrounding_chunks=self._legacy_retriever_value("with_surrounding_chunks", False),
            include_related=settings.include_related,
            include_ancestors=settings.include_ancestors,
            related_limit=self._legacy_retriever_value("related_limit", 10),
            max_ancestor_depth=self._legacy_retriever_value("max_ancestor_depth", None),
        )
        pipeline = RetrieverPipeline(
            retriever=retriever,
            reranker=reranker,
            reranker_top_k=settings.top_n,
            allow_filterless_fallback=self._legacy_retriever_value("allow_filterless_fallback", True),
            rrf_k=settings.rrf_k,
        )
        return PipelineGroup(tuple(partitions), pipeline, settings.top_n, reranker_identity)

    def pipeline_groups(
        self,
        partitions: Sequence[str],
        *,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        disable_reranker: bool = False,
        disable_expansion: bool = False,
    ) -> list[PipelineGroup]:
        """Concrete partitions grouped by the retrieval pipeline they build, each with that pipeline.

        A query embedding only matches vectors of the same model, so partitions
        on different embedders need their own retrieval. Partitions on the same
        vector field (so the ``default`` alias and the embedder behind it stay
        together, as in :meth:`search_groups`) with the same pipeline settings
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
            embedder, _endpoint, _vector_field, search_key = partition_search_identity(partition, configs, endpoints)
            settings = self._pipeline_settings(
                partition_cfg,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
            )
            # A group's pipeline searches with its first partition's embedder, as in search_groups.
            grouped.setdefault((search_key, settings), (embedder, []))[1].append(partition)
        return [self._pipeline(names, embedder, settings) for (_key, settings), (embedder, names) in grouped.items()]


def create_retrieval_service(
    *,
    config: Settings,
    searcher: RetrievalSearcher,
    reranker: Reranker | None = None,
    searcher_factory: Callable[[str], RetrievalSearcher] | None = None,
    reranker_factory: Callable[[str], Reranker] | None = None,
    llm_factory: Callable[[str], LLM] | None = None,
    prompt_service: Any | None = None,
    preset_service: Any | None = None,
) -> RetrievalService:
    """A ``RetrievalService`` over the pipelines these searchers and rerankers build.

    The LLM factory and the prompt service only describe the contextualizer:
    the trace's configuration identity. Without a prompt service (unit tests,
    no DB) the on-disk seed is read.
    """
    service = RetrievalService(
        pipelines=RetrievalPipelineFactory(
            config=config,
            searcher=searcher,
            reranker=reranker,
            searcher_factory=searcher_factory,
            reranker_factory=reranker_factory,
        ),
        identity=RetrievalIdentity(
            config=config,
            prompt_service=prompt_service,
            catalog_llms=llm_factory is not None,
            catalog_rerankers=reranker_factory is not None,
            catalog_embedders=searcher_factory is not None,
            static_reranker=reranker is not None,
        ),
        config=config,
        preset_service=preset_service,
    )
    logger.debug(
        "RetrievalService ready",
        reranker_enabled=config.reranker.enabled and reranker is not None,
        partition_configs=len(getattr(config, "partitions", {}) or {}),
    )
    return service


__all__ = ["RetrievalPipelineFactory", "create_retrieval_service"]
