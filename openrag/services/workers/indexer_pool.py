from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import ray
from services.workers.indexer_actor import IndexerWorker, delete_uploaded_file

from openrag.core.config.root import Settings

# The indexer reloads the DB-backed model-endpoint registry at most once per
# this window (and on a miss), bounding both staleness and DB load regardless
# of indexing throughput.
_MODEL_REGISTRY_TTL_SECONDS = 60.0
_INDEXER_POOL_DISPATCHER_ACTOR_NAME = "IndexerPoolDispatcher"


@ray.remote
class IndexerWorkerActor:
    """Thin Ray actor wrapping ``IndexerWorker`` — one instance per pool slot.

    A worker runs up to ``ray.indexer.max_tasks_per_worker`` files concurrently
    (its Ray ``max_concurrency``). ``IndexerPool`` holds ``ray.indexer.pool_size``
    of these and load-balances across them.
    """

    def __init__(self) -> None:
        import services.inference.ollama_client  # noqa: F401
        import services.inference.vllm_client  # noqa: F401
        from core.config import load_config
        from core.embeddings import embedder_registry
        from core.utils.logging import get_logger
        from services.storage.milvus_store import MilvusVectorStore
        from services.storage.postgres_store import PostgresStore
        from services.workers.parsers.parser_dispatcher import build_caption_vlm, build_parser_dispatcher
        from services.workers.pipeline_builder import build_indexing_pipeline

        cfg = load_config()

        parser = build_parser_dispatcher(cfg)
        parser_factory = _build_parser_factory(parser)
        vlm = build_caption_vlm(cfg)
        chunker = _build_chunker(cfg)
        embedder_factory = _build_embedder_factory(cfg)
        vlm_factory = _build_vlm_factory(cfg)
        contextualizer_factory = _build_contextualizer_factory(cfg)
        topic_tagger_factory = _build_topic_tagger_factory(cfg)

        embed_cfg = cfg.embedder
        embedder = embedder_registry.create(
            "vllm",
            endpoint=embed_cfg.base_url,
            model_name=embed_cfg.model_name,
            api_key=embed_cfg.api_key,
            max_model_len=embed_cfg.max_model_len,
            timeout=embed_cfg.timeout,
            batch_size=embed_cfg.batch_size,
            embed_concurrency=embed_cfg.embed_concurrency,
        )
        self._vector_store = MilvusVectorStore(cfg.vectordb)
        task_state_manager = ray.get_actor("TaskStateManager", namespace="openrag")
        pipeline = build_indexing_pipeline(
            parser=parser,
            chunker=chunker,
            embedder=embedder,
            vector_store=self._vector_store,
            vlm=vlm,
            timeouts=_build_pipeline_timeouts(cfg),
            chunker_factory=_build_chunker_from_config,
            parser_factory=parser_factory,
            embedder_factory=embedder_factory,
            vlm_factory=vlm_factory,
            contextualizer_factory=contextualizer_factory,
            topic_tagger_factory=topic_tagger_factory,
            defer_replace_cleanup=True,
        )
        rdb_cfg = cfg.rdb.model_copy(update={"database": f"partitions_for_collection_{cfg.vectordb.collection_name}"})
        self._catalog_store = PostgresStore(rdb_cfg, run_migrations=False)
        self._catalog_initialized = False
        self._catalog_init_lock = asyncio.Lock()
        # Model-endpoint registry hydration. Unlike the API process, the indexer
        # never runs ModelEndpointService at startup, so cfg.models starts empty
        # and named endpoints (registered via the admin UI) can't resolve here.
        # We hydrate cfg.models from the DB lazily on first use — see
        # _ensure_registry_fresh. The factories above hold a live reference to
        # cfg.models.* so in-place hydration becomes visible to them.
        # Instance-level logger: kept off the module so Ray's by-value pickling
        # of this actor class never drags in loguru's enqueue SimpleQueue (a
        # module-global logger referenced by a method makes the class
        # unpicklable). Created here, in the actor process, it is never pickled.
        self._logger = get_logger()
        self._cfg = cfg
        # Whether "default" resolves via global env/config fallbacks. This keeps
        # reload-on-miss from looping forever when no is_default row exists for a
        # type but the legacy config block can still serve the default endpoint.
        self._has_default_fallbacks = {
            "embedder": _global_embedder_endpoint_config(cfg) is not None,
            "llm": _global_llm_endpoint_config(cfg) is not None,
            "vlm": _global_vlm_endpoint_config(cfg) is not None,
        }
        self._has_default_fallback = self._has_default_fallbacks["llm"]
        self._model_endpoint_service: Any = None
        self._registry_loaded_at: float | None = None
        self._last_miss_reload_at: float | None = None
        self._last_miss_reload_key: tuple[tuple[str, tuple[str, ...]], ...] | None = None
        self._registry_lock = asyncio.Lock()
        self._registry_reload_task: asyncio.Task[None] | None = None
        self._worker = IndexerWorker(
            pipeline=pipeline,
            task_state_manager=task_state_manager,
            document_repo=self._catalog_store.document_repo,
            topic_tag_repo=self._catalog_store.topic_tag_repo,
            vector_store=self._vector_store,
            collection=cfg.vectordb.collection_name,
        )
        # When False (e.g. Twake, which keeps its own copy), the raw upload is
        # purged from ``paths.data_dir`` once indexing settles. Enforced at this
        # actor boundary — not in the worker — so cleanup also covers failures
        # that never reach the worker (catalog/registry init, state updates).
        self._save_uploaded_files = cfg.loader.save_uploaded_files

    async def _ensure_catalog(self) -> None:
        if self._catalog_initialized:
            return
        async with self._catalog_init_lock:
            if self._catalog_initialized:
                return
            await self._catalog_store.initialize()
            self._catalog_initialized = True

    async def _ensure_registry_fresh(self, required_model_names: dict[str, list[str]] | list[str]) -> None:
        """Hydrate ``cfg.models`` from the DB so named endpoints resolve here.

        The hit path is lock-free and does no I/O. A reload happens only on first
        use (``initial``), once the registry goes stale (``ttl``), or when a
        requested name is missing (``miss``, rate-limited to once per window so a
        deleted/typo'd name can't storm the DB). Reloads are single-flight via
        ``_registry_lock``.

        Latency: ``ttl`` refreshes run **in the background** — the current
        registry is still valid, so no file waits on the DB. Only ``initial`` and
        ``miss`` block, because the triggering file needs an endpoint that isn't
        loaded yet; both are rare and rate-limited.
        """
        decision = self._reload_decision(required_model_names)
        if decision is None:
            return
        if decision == "ttl":
            if self._registry_reload_task is None or self._registry_reload_task.done():
                self._registry_reload_task = asyncio.create_task(self._reload_registry(required_model_names))
            return
        await self._reload_registry(required_model_names)

    async def _reload_registry(self, required_model_names: dict[str, list[str]] | list[str]) -> None:
        """Single-flight reload of the model-endpoint registry from the DB."""
        async with self._registry_lock:
            decision = self._reload_decision(required_model_names)
            if decision is None:  # another reload refreshed it while we waited
                return
            try:
                if self._model_endpoint_service is None:
                    from services.orchestrators.model_endpoint_service import ModelEndpointService

                    self._model_endpoint_service = ModelEndpointService(
                        model_endpoint_repo=self._catalog_store.model_endpoint_repo,
                        config=self._cfg,
                    )
                await self._model_endpoint_service.load_all()
            except Exception as exc:  # noqa: BLE001 - a reload must never fail (or crash) a file
                self._logger.warning(f"Model endpoint registry reload failed ({decision}): {exc}")
            # Stamp the clock even on failure so a persistent error degrades to
            # one retry per window rather than one attempt per file.
            now = time.monotonic()
            self._registry_loaded_at = now
            if decision == "miss":
                self._last_miss_reload_at = now
                self._last_miss_reload_key = _required_model_names_key(required_model_names)

    def _reload_decision(self, required_model_names: dict[str, list[str]] | list[str]) -> str | None:
        models = getattr(self._cfg, "models", None)
        required = _normalise_required_model_names(required_model_names)
        missing = False
        for model_type, names in required.items():
            registry = getattr(models, model_type, {}) if models is not None else {}
            has_fallback = _has_default_fallback(self, model_type)
            if any(name not in registry and not (name == "default" and has_fallback) for name in names):
                missing = True
                break
        return _registry_reload_decision(
            loaded_at=self._registry_loaded_at,
            last_miss_at=self._last_miss_reload_at,
            last_miss_key=getattr(self, "_last_miss_reload_key", None),
            missing_key=_required_model_names_key(required_model_names),
            now=time.monotonic(),
            ttl=_MODEL_REGISTRY_TTL_SECONDS,
            missing=missing,
        )

    async def process_file(
        self,
        *,
        task_id: str,
        path: str,
        metadata: dict[str, Any],
        partition: str,
        user: dict[str, Any] | None = None,
        workspace_ids: list[str] | None = None,
        replace: bool = False,
        indexation_config: dict[str, Any] | None = None,
        embedder_name: str | None = None,
        require_existing_partition: bool = False,
    ) -> dict[str, Any]:
        try:
            await self._ensure_catalog()
            await self._ensure_registry_fresh(_required_model_endpoint_names(indexation_config, embedder_name))
            result = await self._worker.process_file(
                task_id=task_id,
                path=path,
                metadata=metadata,
                partition=partition,
                user=user,
                workspace_ids=workspace_ids,
                replace=replace,
                indexation_config=indexation_config,
                embedder_name=embedder_name,
                require_existing_partition=require_existing_partition,
            )
            file_id = metadata.get("file_id", "")
            if workspace_ids and not replace and file_id:
                try:
                    await asyncio.gather(
                        *(
                            self._catalog_store.workspace_repo.add_files_to_workspace(workspace_id, [file_id])
                            for workspace_id in workspace_ids
                        )
                    )
                except Exception:
                    pass
            return result
        finally:
            # Purge the raw upload (when configured) after indexing settles —
            # success or failure. Enforced here rather than in the worker so it
            # also covers pre-processing failures (catalog/registry init, or the
            # SERIALIZING state update) that never enter the worker's try block.
            if not self._save_uploaded_files:
                await delete_uploaded_file(path, self._logger)


@ray.remote
class IndexerPool:
    """Single detached dispatcher actor over a fleet of ``IndexerWorkerActor``.

    Exactly **one** instance exists cluster-wide — it is created named, detached
    and with ``get_if_exists=True`` (see :func:`build_indexer_pool`). Every API
    process and every Ray Serve replica therefore shares the *same* dispatcher,
    so the least-loaded view is global. A per-replica client object would keep
    its own ``_inflight`` counters, and since Ray Serve runs each replica as a
    separate actor/process, those views would diverge and unbalance dispatch
    across the shared workers under bursts.

    It holds ``ray.indexer.pool_size`` worker actors and dispatches each file to
    the least-loaded one (fewest in-flight files). ``submit`` returns the
    worker's Ray ``ObjectRef`` wrapped in a one-element list, so the caller keeps
    per-task cancellation (``ray.cancel``) and task-state tracking. The wrapper
    matters: a *bare* returned ``ObjectRef`` may be auto-dereferenced by Ray
    (blocking ``ray.get`` until the file finishes indexing), whereas a ref nested
    inside a container is returned unresolved — see Ray's nested-task semantics.

    This is an async Ray actor, so ``submit`` and the release callbacks run on a
    single event loop; ``_inflight`` is mutated from that one thread only and
    needs no lock.
    """

    def __init__(self, pool_size: int, max_tasks_per_worker: int, namespace: str = "openrag") -> None:
        if pool_size < 1:
            raise ValueError("IndexerPool requires pool_size >= 1")
        # The workers are themselves named + detached + get_if_exists, so they
        # are shared singletons too; only this dispatcher creates them.
        self._workers = [
            IndexerWorkerActor.options(  # type: ignore[attr-defined]
                name=f"IndexerWorker-{i}",
                namespace=namespace,
                get_if_exists=True,
                lifetime="detached",
                max_concurrency=max_tasks_per_worker,
            ).remote()
            for i in range(pool_size)
        ]
        self._inflight = [0] * len(self._workers)
        self._release_tasks: set[asyncio.Task[Any]] = set()

    async def size(self) -> int:
        return len(self._workers)

    async def submit(self, **kwargs: Any) -> list[Any]:
        """Dispatch ``process_file`` to the least-loaded worker.

        Returns ``[worker_ref]`` (the worker's Ray ``ObjectRef`` in a
        one-element list — see the class docstring); in-flight bookkeeping is
        released when the task settles (success, failure, or cancellation).
        """
        idx = min(range(len(self._workers)), key=self._inflight.__getitem__)
        self._inflight[idx] += 1
        try:
            ref = self._workers[idx].process_file.remote(**kwargs)
        except Exception:
            # Submission failed before a ref exists (e.g. unserializable args or
            # a dead actor); roll back so load balancing stays accurate.
            self._inflight[idx] -= 1
            raise
        task = asyncio.get_running_loop().create_task(self._release(idx, ref))
        # Keep a strong ref so the tracker isn't GC'd mid-flight (asyncio docs).
        self._release_tasks.add(task)
        task.add_done_callback(self._release_tasks.discard)
        return [ref]

    async def _release(self, idx: int, ref: Any) -> None:
        try:
            # return_exceptions=True so a failed/cancelled task still decrements.
            await asyncio.gather(ref, return_exceptions=True)
        finally:
            self._inflight[idx] -= 1


def build_indexer_pool(namespace: str = "openrag") -> Any:
    from core.config import load_config

    cfg = load_config()
    pool_size = cfg.ray.indexer.pool_size
    max_tasks_per_worker = cfg.ray.indexer.max_tasks_per_worker
    # One detached dispatcher actor shared by all API / Serve replicas via
    # get_if_exists. Its own max_concurrency only bounds concurrent submit()
    # calls (each returns promptly without awaiting the worker), so size it to
    # the whole fleet's capacity. The constructor args are honoured only on the
    # first creation; later get_if_exists calls reuse the existing dispatcher
    # and ignore them — which is correct, since every replica loads the same cfg.
    # Do not reuse the old "IndexerPool" name: that detached actor exposed
    # process_file(), while this dispatcher exposes submit().
    return IndexerPool.options(  # type: ignore[attr-defined]
        name=_INDEXER_POOL_DISPATCHER_ACTOR_NAME,
        namespace=namespace,
        get_if_exists=True,
        lifetime="detached",
        max_concurrency=max(1, pool_size * max_tasks_per_worker),
    ).remote(
        pool_size=pool_size,
        max_tasks_per_worker=max_tasks_per_worker,
        namespace=namespace,
    )


def _required_llm_names(indexation_config: dict[str, Any] | None) -> list[str]:
    """LLM endpoint names this file will request, for the reload-on-miss check.

    Mirrors the pipeline's selection logic: contextualization and topic tagging
    each resolve their configured endpoint name (falling back to ``default``).
    """
    if indexation_config is None:
        return []
    names: list[str] = []
    if indexation_config.get("enable_contextualization"):
        names.append(indexation_config.get("contextualization_llm") or "default")
    if indexation_config.get("enable_topic_tagging", False):
        names.append(indexation_config.get("topic_tagging_llm") or "default")
    return names


def _required_model_endpoint_names(
    indexation_config: dict[str, Any] | None,
    embedder_name: str | None,
) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {
        "embedder": [],
        "llm": _required_llm_names(indexation_config),
        "vlm": [],
    }
    if embedder_name:
        names["embedder"].append(embedder_name)
    if indexation_config is not None and indexation_config.get("vlm"):
        names["vlm"].append(str(indexation_config["vlm"]))
    return names


def _normalise_required_model_names(required: dict[str, list[str]] | list[str]) -> dict[str, list[str]]:
    if isinstance(required, list):
        return {"llm": required}
    return required


def _required_model_names_key(required: dict[str, list[str]] | list[str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    normalised = _normalise_required_model_names(required)
    return tuple((model_type, tuple(sorted(set(names)))) for model_type, names in sorted(normalised.items()) if names)


def _has_default_fallback(pool: Any, model_type: str) -> bool:
    fallbacks = getattr(pool, "_has_default_fallbacks", None)
    if fallbacks is not None:
        return bool(fallbacks.get(model_type, False))
    if model_type == "llm":
        return bool(getattr(pool, "_has_default_fallback", False))
    return False


def _registry_reload_decision(
    *,
    loaded_at: float | None,
    last_miss_at: float | None,
    last_miss_key: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
    missing_key: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
    now: float,
    ttl: float,
    missing: bool,
) -> str | None:
    """Decide whether (and why) to reload the model-endpoint registry.

    Returns ``"initial"`` / ``"ttl"`` / ``"miss"``, or ``None`` for the hit path
    (no reload). The ``miss`` branch is rate-limited to once per ``ttl`` so an
    unresolvable name cannot trigger a reload on every file.

    ``miss`` is checked before ``ttl``: a missing required endpoint must trigger
    a *blocking* reload so the current file can resolve it, even when the
    registry is also stale (otherwise the stale-registry ``ttl`` path would
    schedule a background refresh and the file would skip the LLM stage).
    """
    if loaded_at is None:
        return "initial"
    if missing and (last_miss_at is None or last_miss_key != missing_key or now - last_miss_at >= ttl):
        return "miss"
    if now - loaded_at >= ttl:
        return "ttl"
    return None


def _build_chunker(cfg: Any) -> Any:
    from core.chunking.factory import create_chunker

    chunker = create_chunker(cfg)
    if not callable(getattr(chunker, "chunk", None)):
        raise TypeError("Configured chunker does not expose a chunk(document, partition) method")
    return chunker


def _build_chunker_from_config(chunker_config: Any) -> Any:
    return _build_chunker(SimpleNamespace(chunker=chunker_config))


def _build_parser_factory(parser: Any) -> Any:
    """Factory honoring a preset's ``parsing_strategy`` for PDFs.

    Without this, the pipeline falls back to the single global-config dispatcher
    and every PDF uses the global default loader — silently ignoring a preset's
    pymupdf/docling choice. Each per-strategy wrapper reuses ``parser``'s shared
    backend cache, so selecting a strategy never builds a duplicate
    marker/docling Ray pool.
    """
    cache: dict[str, Any] = {}

    def factory(strategy: str = "marker") -> Any:
        wrapper = cache.get(strategy)
        if wrapper is None:
            wrapper = parser.for_pdf_strategy(strategy)
            cache[strategy] = wrapper
        return wrapper

    return factory


def _build_pipeline_timeouts(cfg: Settings) -> Any:
    """Per-stage timeouts for the indexing pipeline.

    Bounds the parse stage at ``loader.parse_timeout`` so a wedged parse (notably
    pymupdf, which has no internal timeout) fails that file instead of stalling
    indexing. Other stages stay unbounded here (their backends self-limit). See #571.
    """
    from services.workers.pipeline_builder import PipelineTimeouts

    return PipelineTimeouts(parse=cfg.loader.parse_timeout)


def _build_embedder_factory(cfg: Settings) -> Any:
    from core.embeddings import embedder_registry

    models = getattr(cfg, "models", None)
    named_embedders = models.embedder if models is not None else {}
    fallback_cfg = _global_embedder_endpoint_config(cfg)

    cache: dict[str, tuple[str, Any]] = {}
    lock = threading.Lock()

    def factory(name: str = "default") -> Any:
        model_cfg = named_embedders.get(name)
        if model_cfg is None:
            if name == "default" and fallback_cfg is not None:
                model_cfg = fallback_cfg
            else:
                raise KeyError(f"Unknown embedder '{name}'. Available: {list(named_embedders)}")
        identity = _endpoint_identity(model_cfg)
        entry = cache.get(name)
        if entry is not None and entry[0] == identity:
            return entry[1]
        with lock:
            entry = cache.get(name)
            if entry is not None and entry[0] == identity:
                return entry[1]
            impl_kwargs = {key: value for key, value in model_cfg.extra.items() if key != "implementation"}
            impl = model_cfg.extra.get("implementation", "vllm")
            # Backfill max_model_len/embed_concurrency from static settings when the
            # endpoint's `extra` omits them — otherwise truncate_prompt_tokens is off
            # and pooling models hang/400 on boundary inputs (vllm#29496). Explicit
            # `extra` wins. Mirrors the API container's _embedder_extra_kwargs.
            embed_defaults = getattr(cfg, "embedder", None)
            for default_key in ("max_model_len", "embed_concurrency"):
                default = getattr(embed_defaults, default_key, None)
                if default is not None:
                    impl_kwargs.setdefault(default_key, default)
            instance = embedder_registry.create(
                impl,
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
                batch_size=model_cfg.batch_size,
                timeout=model_cfg.timeout,
                **impl_kwargs,
            )
            cache[name] = (identity, instance)
            return instance

    return factory


def _build_vlm_factory(cfg: Settings) -> Any:
    import services.inference.vllm_client  # noqa: F401
    from core.vlm import vlm_registry

    models = getattr(cfg, "models", None)
    named_vlms = models.vlm if models is not None else {}
    fallback_cfg = _global_vlm_endpoint_config(cfg)

    cache: dict[str, tuple[str, Any]] = {}
    lock = threading.Lock()

    def factory(name: str = "default") -> Any:
        model_cfg = named_vlms.get(name)
        if model_cfg is None:
            if name == "default" and fallback_cfg is not None:
                model_cfg = fallback_cfg
            else:
                raise KeyError(f"Unknown vlm '{name}'. Available: {list(named_vlms)}")
        identity = _endpoint_identity(model_cfg)
        entry = cache.get(name)
        if entry is not None and entry[0] == identity:
            return entry[1]
        with lock:
            entry = cache.get(name)
            if entry is not None and entry[0] == identity:
                return entry[1]
            impl_kwargs = {key: value for key, value in model_cfg.extra.items() if key != "implementation"}
            impl = model_cfg.extra.get("implementation", "vllm")
            instance = vlm_registry.create(
                impl,
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
                timeout=model_cfg.timeout,
                **impl_kwargs,
            )
            cache[name] = (identity, instance)
            return instance

    return factory


def _build_contextualizer_factory(cfg: Settings) -> Any:
    """Build a cached factory yielding ``ChunkContextualizer`` instances.

    Each requested LLM name is resolved at call time against the named-endpoint
    registry (``cfg.models.llm``), falling back to the global ``cfg.llm`` block
    for the ``default`` name. The registry is hydrated from the DB lazily by the
    indexer actor *after* this factory is built, so we hold a live reference to
    ``cfg.models.llm`` rather than a snapshot. A name with no entry and no
    fallback raises ``KeyError`` — the pipeline catches it and skips the stage.
    One client is cached per name and rebuilt when that endpoint's full config
    (URL, model, or any ``extra`` such as the api_key) changes, so an edit yields
    a fresh client after the registry reloads. The superseded client is dropped
    from the cache but not explicitly closed — acceptable since endpoint edits
    are rare and the actor reclaims it on exit. Every contextualizer shares the
    cluster-wide ``llmSemaphore`` so contextualization LLM calls obey the same
    global concurrency limit as query-time calls.
    """
    import services.inference.ollama_client  # noqa: F401
    import services.inference.vllm_client  # noqa: F401
    from core.indexing.contextualize import ChunkContextualizer
    from core.llm import llm_registry
    from core.prompts import load_template_by_key
    from services.inference.distributed_semaphore import DistributedSemaphore

    models = getattr(cfg, "models", None)
    named_llms = models.llm if models is not None else {}
    fallback_cfg = _global_llm_endpoint_config(cfg)

    # name -> (endpoint-identity, instance). Bounded to one entry per name; a
    # changed identity replaces (not accumulates) the cached client.
    cache: dict[str, tuple[str, ChunkContextualizer]] = {}
    # Prompt + semaphore are built lazily on first successful resolve so a pool
    # with no resolvable LLM does no startup work (and an unknown name raises
    # before touching them).
    shared: dict[str, Any] = {}
    lock = threading.Lock()

    def factory(name: str = "default") -> ChunkContextualizer:
        model_cfg = named_llms.get(name)
        if model_cfg is None:
            if name == "default" and fallback_cfg is not None:
                model_cfg = fallback_cfg
            else:
                raise KeyError(f"Unknown llm '{name}'. Available: {list(named_llms)}")
        identity = _endpoint_identity(model_cfg)
        entry = cache.get(name)
        if entry is not None and entry[0] == identity:
            return entry[1]
        with lock:
            entry = cache.get(name)
            if entry is not None and entry[0] == identity:
                return entry[1]
            if "system_prompt" not in shared:
                # Build both before publishing to `shared` so a failure can't
                # leave it half-initialised (which would later raise a stray
                # KeyError on the missing key, misread as an unresolvable LLM).
                system_prompt = load_template_by_key(cfg.paths.prompts_dir, cfg.prompts, "chunk_contextualizer")
                # Built directly from config to avoid importing
                # services.inference.runtime, which eagerly constructs a LangDetector.
                llm_semaphore = DistributedSemaphore(
                    name="llmSemaphore", max_concurrent_ops=cfg.semaphore.llm_semaphore
                )
                shared["system_prompt"] = system_prompt
                shared["llm_semaphore"] = llm_semaphore
            impl_kwargs = {key: value for key, value in model_cfg.extra.items() if key != "implementation"}
            impl = model_cfg.extra.get("implementation", "vllm")
            llm = llm_registry.create(
                impl,
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
                timeout=model_cfg.timeout,
                **impl_kwargs,
            )
            contextualizer = ChunkContextualizer(
                llm,
                shared["system_prompt"],
                timeout_seconds=cfg.chunker.contextualization_timeout,
                batch_size=cfg.chunker.max_concurrent_contextualization,
                llm_semaphore=shared["llm_semaphore"],
            )
            cache[name] = (identity, contextualizer)
            return contextualizer

    return factory


def _build_topic_tagger_factory(cfg: Settings) -> Any:
    """Build a cached factory yielding ``TopicTagger`` instances.

    Mirrors :func:`_build_contextualizer_factory`: a live reference to the
    DB-hydrated ``cfg.models.llm`` registry, global fallback for ``default``,
    a ``KeyError`` on an unresolvable name (skipped by the pipeline), a
    one-entry-per-name client cache replaced on endpoint change, and a lazily
    loaded prompt.
    """
    import services.inference.ollama_client  # noqa: F401
    import services.inference.vllm_client  # noqa: F401
    from core.indexing.topic_tags import TopicTagger
    from core.llm import llm_registry
    from core.prompts import load_template_by_key

    models = getattr(cfg, "models", None)
    named_llms = models.llm if models is not None else {}
    fallback_cfg = _global_llm_endpoint_config(cfg)

    cache: dict[str, tuple[str, TopicTagger]] = {}
    shared: dict[str, Any] = {}
    lock = threading.Lock()

    def factory(name: str = "default") -> TopicTagger:
        model_cfg = named_llms.get(name)
        if model_cfg is None:
            if name == "default" and fallback_cfg is not None:
                model_cfg = fallback_cfg
            else:
                raise KeyError(f"Unknown llm '{name}'. Available: {list(named_llms)}")
        identity = _endpoint_identity(model_cfg)
        entry = cache.get(name)
        if entry is not None and entry[0] == identity:
            return entry[1]
        with lock:
            entry = cache.get(name)
            if entry is not None and entry[0] == identity:
                return entry[1]
            if "system_prompt" not in shared:
                shared["system_prompt"] = load_template_by_key(cfg.paths.prompts_dir, cfg.prompts, "topic_tagger")
            impl_kwargs = {key: value for key, value in model_cfg.extra.items() if key != "implementation"}
            impl = model_cfg.extra.get("implementation", "vllm")
            llm = llm_registry.create(
                impl,
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
                timeout=model_cfg.timeout,
                **impl_kwargs,
            )
            tagger = TopicTagger(llm, shared["system_prompt"], timeout_seconds=model_cfg.timeout)
            cache[name] = (identity, tagger)
            return tagger

    return factory


def _endpoint_identity(model_cfg: Any) -> str:
    """Stable identity of a resolved endpoint config for client-cache keying.

    Covers the full config — endpoint, model **and** every ``extra`` key
    (``api_key``, ``temperature``, ...) — so any edit (including an api-key
    rotation that keeps the same URL/model) yields a new identity and rebuilds
    the cached client on the next reload, matching the API process's
    invalidate-on-any-change behaviour.
    """
    import hashlib
    import json

    payload = json.dumps(model_cfg.model_dump(mode="json"), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _global_llm_endpoint_config(cfg: Any) -> Any | None:
    """Adapt the legacy/global ``cfg.llm`` block into a ``ModelEndpointConfig``.

    Returns ``None`` when the global LLM endpoint or model is unset, signalling
    that no ``default`` contextualizer can be built from the global config.
    """
    from core.config.model_endpoints import ModelEndpointConfig

    llm_cfg = getattr(cfg, "llm", None)
    endpoint = getattr(llm_cfg, "base_url", "")
    model_name = getattr(llm_cfg, "model", "")
    if not endpoint or not model_name:
        return None
    extra = {
        "implementation": "vllm",
        "api_key": getattr(llm_cfg, "api_key", ""),
        "temperature": getattr(llm_cfg, "temperature", 0.1),
        "max_retries": getattr(llm_cfg, "max_retries", 2),
        "logprobs": getattr(llm_cfg, "logprobs", True),
    }
    enable_thinking = getattr(llm_cfg, "enable_thinking", None)
    if enable_thinking is not None:
        extra["enable_thinking"] = enable_thinking
    return ModelEndpointConfig(
        endpoint=endpoint,
        model_name=model_name,
        timeout=getattr(llm_cfg, "timeout", 60),
        extra=extra,
    )


def _global_embedder_endpoint_config(cfg: Any) -> Any | None:
    from core.config.model_endpoints import ModelEndpointConfig

    embed_cfg = getattr(cfg, "embedder", None)
    endpoint = getattr(embed_cfg, "base_url", "")
    model_name = getattr(embed_cfg, "model_name", "")
    if not endpoint or not model_name:
        return None
    return ModelEndpointConfig(
        endpoint=endpoint,
        model_name=model_name,
        batch_size=getattr(embed_cfg, "batch_size", 32),
        timeout=getattr(embed_cfg, "timeout", 120),
        extra={
            "implementation": "vllm",
            "api_key": getattr(embed_cfg, "api_key", ""),
            "max_model_len": getattr(embed_cfg, "max_model_len", None),
            "embed_concurrency": getattr(embed_cfg, "embed_concurrency", 4),
        },
    )


def _global_vlm_endpoint_config(cfg: Any) -> Any | None:
    from core.config.model_endpoints import ModelEndpointConfig

    vlm_cfg = getattr(cfg, "vlm", None)
    endpoint = getattr(vlm_cfg, "base_url", "")
    model_name = getattr(vlm_cfg, "model", "")
    if not endpoint or not model_name:
        return None
    extra = {
        "implementation": "vllm",
        "api_key": getattr(vlm_cfg, "api_key", ""),
    }
    enable_thinking = getattr(vlm_cfg, "enable_thinking", None)
    if enable_thinking is not None:
        extra["enable_thinking"] = enable_thinking
    return ModelEndpointConfig(
        endpoint=endpoint,
        model_name=model_name,
        timeout=getattr(vlm_cfg, "timeout", 60),
        extra=extra,
    )


__all__ = ["IndexerPool", "IndexerWorkerActor", "build_indexer_pool"]
