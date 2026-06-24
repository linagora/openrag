from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import ray
from services.workers.indexer_actor import IndexerWorker

from openrag.core.config.root import Settings

# The indexer reloads the DB-backed model-endpoint registry at most once per
# this window (and on a miss), bounding both staleness and DB load regardless
# of indexing throughput.
_MODEL_REGISTRY_TTL_SECONDS = 60.0


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
        vlm = build_caption_vlm(cfg)
        chunker = _build_chunker(cfg)
        embedder_factory = _build_embedder_factory(cfg)
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
            image_captioning=cfg.loader.image_captioning,
            chunker_factory=_build_chunker_from_config,
            embedder_factory=embedder_factory,
            contextualizer_factory=contextualizer_factory,
            topic_tagger_factory=topic_tagger_factory,
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
        # Whether "default" resolves via the global cfg.llm fallback (static —
        # cfg.llm doesn't change). Lets _reload_decision avoid a perpetual
        # reload-on-miss for "default" when no is_default row exists in the DB.
        self._has_default_fallback = _global_llm_endpoint_config(cfg) is not None
        self._model_endpoint_service: Any = None
        self._registry_loaded_at: float | None = None
        self._last_miss_reload_at: float | None = None
        self._registry_lock = asyncio.Lock()
        self._registry_reload_task: asyncio.Task[None] | None = None
        self._worker = IndexerWorker(
            pipeline=pipeline,
            task_state_manager=task_state_manager,
            document_repo=self._catalog_store.document_repo,
            topic_tag_repo=self._catalog_store.topic_tag_repo,
        )

    async def _ensure_catalog(self) -> None:
        if self._catalog_initialized:
            return
        async with self._catalog_init_lock:
            if self._catalog_initialized:
                return
            await self._catalog_store.initialize()
            self._catalog_initialized = True

    async def _ensure_registry_fresh(self, required_llm_names: list[str]) -> None:
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
        decision = self._reload_decision(required_llm_names)
        if decision is None:
            return
        if decision == "ttl":
            if self._registry_reload_task is None or self._registry_reload_task.done():
                self._registry_reload_task = asyncio.create_task(self._reload_registry(required_llm_names))
            return
        await self._reload_registry(required_llm_names)

    async def _reload_registry(self, required_llm_names: list[str]) -> None:
        """Single-flight reload of the model-endpoint registry from the DB."""
        async with self._registry_lock:
            decision = self._reload_decision(required_llm_names)
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

    def _reload_decision(self, required_llm_names: list[str]) -> str | None:
        models = getattr(self._cfg, "models", None)
        llm_registry = getattr(models, "llm", {}) if models is not None else {}
        # The factory also resolves "default" via the global cfg.llm fallback, even
        # when no is_default row puts a "default" alias in the registry. Treating it
        # as missing in that case would block-reload every window forever without
        # ever converging, so mirror the factory's fallback here.
        missing = any(
            name not in llm_registry and not (name == "default" and self._has_default_fallback)
            for name in required_llm_names
        )
        return _registry_reload_decision(
            loaded_at=self._registry_loaded_at,
            last_miss_at=self._last_miss_reload_at,
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
    ) -> dict[str, Any]:
        await self._ensure_catalog()
        await self._ensure_registry_fresh(_required_llm_names(indexation_config))
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


class IndexerPool:
    """Client-side pool of ``IndexerWorkerActor`` handles.

    Holds ``ray.indexer.pool_size`` worker actors and dispatches each file to
    the least-loaded one (fewest in-flight files). ``submit`` returns the Ray
    ``ObjectRef`` unchanged so the dispatcher keeps per-task cancellation
    (``ray.cancel``) and state tracking — this is why the pool is a plain
    client object rather than ``ray.util.ActorPool``, which hides the ref.

    In-flight counts are mutated only on the event-loop thread (``submit``
    and the release callback), so no lock is needed.
    """

    def __init__(self, workers: list[Any]) -> None:
        if not workers:
            raise ValueError("IndexerPool requires at least one worker actor")
        self._workers = list(workers)
        self._inflight = [0] * len(self._workers)
        self._release_tasks: set[asyncio.Task[Any]] = set()

    @property
    def size(self) -> int:
        return len(self._workers)

    def submit(self, **kwargs: Any) -> Any:
        """Dispatch ``process_file`` to the least-loaded worker.

        Must be called from within the event loop. Returns the worker's
        Ray ``ObjectRef``; in-flight bookkeeping is released when the task
        settles (success, failure, or cancellation).
        """
        idx = min(range(len(self._workers)), key=self._inflight.__getitem__)
        self._inflight[idx] += 1
        ref = self._workers[idx].process_file.remote(**kwargs)
        task = asyncio.get_running_loop().create_task(self._release(idx, ref))
        # Keep a strong ref so the tracker isn't GC'd mid-flight (asyncio docs).
        self._release_tasks.add(task)
        task.add_done_callback(self._release_tasks.discard)
        return ref

    async def _release(self, idx: int, ref: Any) -> None:
        try:
            # return_exceptions=True so a failed/cancelled task still decrements.
            await asyncio.gather(ref, return_exceptions=True)
        finally:
            self._inflight[idx] -= 1


def build_indexer_pool(namespace: str = "openrag") -> IndexerPool:
    from core.config import load_config

    cfg = load_config()
    pool_size = cfg.ray.indexer.pool_size
    max_concurrency = cfg.ray.indexer.max_tasks_per_worker
    workers = [
        IndexerWorkerActor.options(  # type: ignore[attr-defined]
            name=f"IndexerWorker-{i}",
            namespace=namespace,
            get_if_exists=True,
            lifetime="detached",
            max_concurrency=max_concurrency,
        ).remote()
        for i in range(pool_size)
    ]
    return IndexerPool(workers)


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
    if indexation_config.get("enable_topic_tagging", True):
        names.append(indexation_config.get("topic_tagging_llm") or "default")
    return names


def _registry_reload_decision(
    *,
    loaded_at: float | None,
    last_miss_at: float | None,
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
    if missing and (last_miss_at is None or now - last_miss_at >= ttl):
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


def _build_embedder_factory(cfg: Settings) -> Any:
    if not getattr(cfg.models, "embedder", None):
        return None

    from core.embeddings import embedder_registry

    cache: dict[str, Any] = {}
    lock = threading.Lock()

    def factory(name: str = "default") -> Any:
        if name in cache:
            return cache[name]
        with lock:
            if name in cache:
                return cache[name]
            model_cfg = cfg.models.embedder.get(name)
            if model_cfg is None:
                raise KeyError(f"Unknown embedder '{name}'. Available: {list(cfg.models.embedder)}")
            impl_kwargs = {key: value for key, value in model_cfg.extra.items() if key != "implementation"}
            impl = model_cfg.extra.get("implementation", "vllm")
            instance = embedder_registry.create(
                impl,
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
                batch_size=model_cfg.batch_size,
                timeout=model_cfg.timeout,
                **impl_kwargs,
            )
            cache[name] = instance
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


__all__ = ["IndexerPool", "IndexerWorkerActor", "build_indexer_pool"]
