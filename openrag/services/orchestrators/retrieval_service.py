"""RetrievalService — retrieval orchestration.

Fans a query out over the groups of partitions that share a retrieval
pipeline, and fuses the groups' results with RRF. Which pipeline, searcher
and reranker serve which partitions is the ``PipelineFactory`` port
(``core.retrieval.pipeline_factory``), implemented in ``di/retrieval.py``,
which also builds this service. It also serves the raw ``/search`` endpoint.
The description of the retrieval settings for the trace and its fingerprints
is ``RetrievalIdentity`` (``retrieval_identity.py``); this service adds what
resolving the pipelines tells.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.models.retrieval_trace import TraceRemovalReason
from core.retrieval.pipeline import RetrieverPipeline, _rerank_chunks, _safe_trace, _with_removal_reasons
from core.retrieval.retriever import _expand_with_related_chunks
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
from core.utils.exceptions import ServiceUnavailableError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.models.chunk import Chunk
    from core.models.query import Query, SearchQueries
    from core.rerankers.reranker import Reranker
    from core.retrieval.pipeline_factory import PipelineFactory
    from core.retrieval.searcher import RetrievalSearcher
    from services.orchestrators.retrieval_identity import RetrievalIdentity

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
    """Retrieval pipeline orchestration (search, single/multi retrieve).

    Built by ``di.retrieval.create_retrieval_service``.
    """

    def __init__(
        self,
        *,
        pipelines: PipelineFactory,
        identity: RetrievalIdentity,
        config: Settings,
        preset_service: Any | None = None,
    ) -> None:
        self._pipelines = pipelines
        self._identity = identity
        # Live reference: the partition configs are refreshed on every preset change.
        self._config = config
        self._preset_service = preset_service

    def _partition_configs(self) -> dict[str, Any]:
        return getattr(self._config, "partitions", {}) or {}

    async def refresh_partition_configs(self) -> None:
        """Reload partition settings another process changed, such as the embedder a swap switched."""
        if self._preset_service is not None:
            await self._preset_service.try_refresh_if_stale()

    def has_loaded_partition_retrieval_config(self, partition: str) -> bool:
        """Return whether this process has resolved retrieval settings for a partition."""
        return partition in self._partition_configs()

    def public_retrieval_configuration(self, partitions: Sequence[str]) -> dict[str, object]:
        """Build the stable public retrieval-setting subset used for fingerprints."""
        return self._identity.public_configuration(partitions)

    def configuration_fingerprint(self, partitions: Sequence[str]) -> str:
        """Fingerprint only allowlisted public settings for the authorized scope."""
        return self._identity.fingerprint(partitions)

    def public_search_configuration(
        self,
        partitions: Sequence[str],
        effective_options: Mapping[str, object],
    ) -> dict[str, object]:
        """Return only settings that can affect the raw search endpoint."""
        rerank = None
        if effective_options.get("rerank"):
            # What the rerank ran with, resolved as search() does: the request
            # holds rerank_candidates as sent (None when it follows the preset).
            rerank_partitions = list(partitions)
            _reranker, reranker_identity = self._search_reranker(rerank_partitions)
            rerank = {
                "reranker": reranker_identity,
                "candidates": self._search_rerank_candidates(
                    rerank_partitions, effective_options["top_k"], effective_options.get("rerank_candidates")
                ),
            }
        return self._identity.public_search_configuration(partitions, effective_options, rerank=rerank)

    def search_configuration_fingerprint(
        self,
        partitions: Sequence[str],
        effective_options: Mapping[str, object],
    ) -> str:
        """Fingerprint the effective raw-search path without chat-only settings."""
        return canonical_fingerprint(self.public_search_configuration(partitions, effective_options))

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
        selected = list(configs) if "all" in requested else list(dict.fromkeys(requested))
        reranker_identities: dict[str, dict[str, object]] = {}
        groups: list[ResolvedRetrievalGroup] = []

        if not build_execution:
            for partition_name in selected:
                partition = configs.get(partition_name)
                retrieval = partition.retrieval if partition is not None else self._config.retriever
                reranker_enabled = getattr(
                    retrieval,
                    "enable_reranker",
                    getattr(getattr(self._config, "reranker", None), "enabled", False),
                )
                if partition is not None and reranker_enabled and not disable_reranker:
                    _reranker, identity = self._pipelines.reranker(
                        getattr(retrieval, "reranker", None),
                        partition_name,
                    )
                    reranker_identities[partition_name] = identity
        elif selected:
            for group in self._pipelines.pipeline_groups(
                selected,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
            ):
                groups.append(ResolvedRetrievalGroup(tuple(group.partitions), group.pipeline, group.default_top_k))
                # Every partition of a group shares the reranker it was grouped by.
                if group.reranker_identity is not None:
                    for partition_name in group.partitions:
                        reranker_identities[partition_name] = group.reranker_identity
            if len(groups) == 1:
                # Keep the requested scope, so "all" stays unscoped, as in _pipeline_groups_for_partitions.
                (group,) = groups
                groups = [
                    ResolvedRetrievalGroup(
                        tuple(["all"] if "all" in requested else selected), group.pipeline, group.default_top_k
                    )
                ]

        public, fingerprint = await self._identity.resolved_configuration(
            requested,
            reranker_identities=reranker_identities,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            disable_reranker=disable_reranker,
            disable_expansion=disable_expansion,
            contextualizer_prompt=contextualizer_prompt,
            strict_prompt_resolution=strict_prompt_resolution,
        )
        return ResolvedRetrievalPlan(
            groups=tuple(groups),
            public_configuration=public,
            configuration_fingerprint=fingerprint,
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

        See ``PipelineFactory.pipeline_groups`` for the grouping. The groups'
        lists are fused with RRF by the caller.

        ``"all"`` only reaches this layer post-authorization (a SUPER_ADMIN_MODE
        admin; regular users are already expanded to their memberships upstream),
        so every hydrated partition is in scope. With a single group it is kept,
        so the search stays unscoped, as in ``PipelineFactory.search_groups``.
        """
        await self.refresh_partition_configs()
        configs = self._partition_configs()
        scope = list(configs) if "all" in partitions else partitions
        if not scope:
            # No partition to retrieve from: nothing to search.
            return []
        groups = [
            (group.partitions, group.pipeline, group.default_top_k)
            for group in self._pipelines.pipeline_groups(
                scope,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                disable_reranker=disable_reranker,
                disable_expansion=disable_expansion,
            )
        ]
        if len(groups) == 1:
            # Keep the caller's partitions, so "all" stays unscoped.
            _, pipeline, default_top_k = groups[0]
            return [(partitions, pipeline, default_top_k)]
        return groups

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
        reranker, identity = self._pipelines.reranker(
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
        groups = self._pipelines.search_groups(parts)
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
