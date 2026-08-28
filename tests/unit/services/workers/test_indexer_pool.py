from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.config.model_endpoints import ModelEndpointConfig
from core.models.catalog import CONTENT_CLAIM_TOKEN_METADATA_KEY


class _NativeChunker:
    def chunk(self, document, partition: str = "default"):
        return []


class _BrokenChunker:
    pass


class _NonCallableChunker:
    chunk = None


def test_build_pipeline_timeouts_bounds_parse_from_config() -> None:
    """The pipeline must bound the parse stage at loader.parse_timeout so a wedged
    parse fails that file instead of stalling indexing (#571)."""
    from services.workers.indexer_pool import _build_pipeline_timeouts

    cfg = SimpleNamespace(loader=SimpleNamespace(parse_timeout=42))

    timeouts = _build_pipeline_timeouts(cfg)

    assert timeouts.parse == 42


def test_build_chunker_returns_native_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.chunking.factory as factory
    from services.workers.indexer_pool import _build_chunker

    native = _NativeChunker()
    monkeypatch.setattr(factory, "create_chunker", lambda _cfg, _window=None: native)

    assert _build_chunker(object()) is native


def test_build_chunker_rejects_invalid_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.chunking.factory as factory
    from services.workers.indexer_pool import _build_chunker

    monkeypatch.setattr(factory, "create_chunker", lambda _cfg, _window=None: _BrokenChunker())

    with pytest.raises(TypeError, match="chunk"):
        _build_chunker(object())


def test_build_chunker_rejects_non_callable_chunk_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.chunking.factory as factory
    from services.workers.indexer_pool import _build_chunker

    monkeypatch.setattr(factory, "create_chunker", lambda _cfg, _window=None: _NonCallableChunker())

    with pytest.raises(TypeError, match="chunk"):
        _build_chunker(object())


@pytest.mark.asyncio
async def test_catalog_initialization_is_single_flight() -> None:
    from services.workers.indexer_pool import IndexerWorkerActor

    actor_class = IndexerWorkerActor.__ray_metadata__.modified_class
    pool = actor_class.__new__(actor_class)

    class Store:
        def __init__(self) -> None:
            self.calls = 0

        async def initialize(self) -> None:
            self.calls += 1
            await asyncio.sleep(0)

    store = Store()
    pool._catalog_store = store
    pool._catalog_initialized = False
    pool._catalog_init_lock = asyncio.Lock()

    await asyncio.gather(*(pool._ensure_catalog() for _ in range(20)))

    assert store.calls == 1
    assert pool._catalog_initialized is True


def test_build_indexer_pool_uses_current_protocol_dispatcher_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.config
    import services.workers.indexer_pool as module

    options_calls = []
    remote_calls = []

    class Options:
        def __init__(self, kwargs):
            self._kwargs = kwargs

        def remote(self, **rkwargs):
            remote_calls.append(rkwargs)
            return "dispatcher-actor"

    def fake_options(**kwargs):
        options_calls.append(kwargs)
        return Options(kwargs)

    cfg = SimpleNamespace(ray=SimpleNamespace(indexer=SimpleNamespace(pool_size=3, max_tasks_per_worker=4)))
    monkeypatch.setattr(core.config, "load_config", lambda: cfg)
    monkeypatch.setattr(module.IndexerPool, "options", fake_options)

    pool = module.build_indexer_pool()

    # A single shared dispatcher actor — not one client object per replica.
    assert pool == "dispatcher-actor"
    assert len(options_calls) == 1
    opts = options_calls[0]
    # A protocol-specific name prevents a rolling deployment from attaching to
    # a detached actor that still runs the previous claim implementation.
    assert opts["name"] == "IndexerPoolDispatcher-v5"
    assert opts["namespace"] == "openrag"
    assert opts["get_if_exists"] is True
    assert opts["lifetime"] == "detached"
    # max_concurrency bounds concurrent submit() calls → whole-fleet capacity.
    assert opts["max_concurrency"] == 12
    # pool_size / max_tasks_per_worker are passed to the actor constructor.
    assert remote_calls == [{"pool_size": 3, "max_tasks_per_worker": 4, "namespace": "openrag"}]


def test_indexer_pool_actor_spawns_pool_size_detached_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.workers.indexer_pool as module

    calls = []

    class Options:
        def __init__(self, kwargs):
            self._kwargs = kwargs

        def remote(self):
            return f"actor-{self._kwargs['name']}"

    def fake_options(**kwargs):
        calls.append(kwargs)
        return Options(kwargs)

    monkeypatch.setattr(module.IndexerWorkerActor, "options", fake_options)

    actor_class = module.IndexerPool.__ray_metadata__.modified_class
    pool = actor_class(pool_size=3, max_tasks_per_worker=4)

    # One detached worker actor per pool_size slot, each capped at max_tasks_per_worker.
    assert len(pool._workers) == 3
    assert {c["name"] for c in calls} == {
        "IndexerWorker-v5-0",
        "IndexerWorker-v5-1",
        "IndexerWorker-v5-2",
    }
    for c in calls:
        assert c["lifetime"] == "detached"
        assert c["max_concurrency"] == 4
        assert c["get_if_exists"] is True
        assert c["namespace"] == "openrag"


def test_build_topic_tagger_factory_resolves_named_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.llm import llm_registry
    from services.workers.indexer_pool import _build_topic_tagger_factory

    class ProbeLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    llm_registry.register("topic-probe")(ProbeLLM)
    cfg = SimpleNamespace(
        models=SimpleNamespace(
            llm={
                "topic-a": ModelEndpointConfig(
                    endpoint="http://llm:8000/v1",
                    model_name="topic-model",
                    timeout=9.0,
                    extra={"implementation": "topic-probe", "temperature": 0.1},
                )
            }
        ),
        llm=SimpleNamespace(base_url="", model=""),
        paths=SimpleNamespace(prompts_dir="/tmp/prompts"),
        prompts=SimpleNamespace(topic_tagger="topic.txt"),
    )
    monkeypatch.setattr("core.prompts.load_template_by_key", lambda *_args: "extract topics")

    factory = _build_topic_tagger_factory(cfg)
    tagger = factory("topic-a")

    assert tagger._llm.kwargs["endpoint"] == "http://llm:8000/v1"
    assert tagger._llm.kwargs["model_name"] == "topic-model"
    assert tagger._llm.kwargs["temperature"] == 0.1


def test_worker_factories_do_not_forward_the_env_managed_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """`managed_by` is bookkeeping, not a constructor kwarg — and never a request field.

    Every seeded endpoint now carries the marker in `extra`, and the worker
    factories splat `extra` straight into the client. Forwarding it would push
    `managed_by: "env"` into the provider payload, which a strict
    OpenAI-compatible service rejects with a 400 — failing indexing rather than
    anything visibly related to the marker.
    """
    from core.config.model_endpoints import (
        ENV_MANAGED_KEY,
        ENV_MANAGED_VALUE,
        LLM_CONTEXT_SIZE_KEY,
        LLM_OUTPUT_TOKENS_KEY,
    )
    from core.llm import llm_registry
    from services.workers.indexer_pool import _build_topic_tagger_factory

    class ProbeLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    llm_registry.register("marker-probe")(ProbeLLM)
    cfg = SimpleNamespace(
        models=SimpleNamespace(
            llm={
                "topic-a": ModelEndpointConfig(
                    endpoint="http://llm:8000/v1",
                    model_name="topic-model",
                    timeout=9.0,
                    extra={
                        "implementation": "marker-probe",
                        "temperature": 0.1,
                        ENV_MANAGED_KEY: ENV_MANAGED_VALUE,
                        LLM_CONTEXT_SIZE_KEY: 8192,
                        LLM_OUTPUT_TOKENS_KEY: 1024,
                    },
                )
            }
        ),
        llm=SimpleNamespace(base_url="", model=""),
        paths=SimpleNamespace(prompts_dir="/tmp/prompts"),
        prompts=SimpleNamespace(topic_tagger="topic.txt"),
    )
    monkeypatch.setattr("core.prompts.load_template_by_key", lambda *_args: "extract topics")

    tagger = _build_topic_tagger_factory(cfg)("topic-a")

    assert ENV_MANAGED_KEY not in tagger._llm.kwargs
    assert "implementation" not in tagger._llm.kwargs
    # The LLM token budgets are the same class of control key. di/factories.py
    # already stripped them, but these worker factories did not — so they leaked
    # into every worker-issued request until both sides shared one set.
    assert LLM_CONTEXT_SIZE_KEY not in tagger._llm.kwargs
    assert LLM_OUTPUT_TOKENS_KEY not in tagger._llm.kwargs
    assert tagger._llm.kwargs["temperature"] == 0.1  # real kwargs still forwarded


def test_build_contextualizer_factory_returns_factory_for_later_hydration(tmp_path) -> None:
    # With no LLM configured at build time the factory is still returned (the
    # registry is hydrated from the DB later) — resolving an unknown name raises
    # KeyError, which the pipeline catches and skips. It must raise *before*
    # touching the prompt/semaphore, so neither is needed in this cfg.
    from services.workers.indexer_pool import _build_contextualizer_factory

    cfg = SimpleNamespace(
        models=SimpleNamespace(llm={}),
        llm=SimpleNamespace(base_url="", model="", api_key=""),
        chunker=SimpleNamespace(contextualization_timeout=12, max_concurrent_contextualization=3),
        paths=SimpleNamespace(prompts_dir=str(tmp_path)),
        prompts=SimpleNamespace(chunk_contextualizer="chunk_contextualizer_tmpl.txt"),
    )

    factory = _build_contextualizer_factory(cfg)
    assert factory is not None
    with pytest.raises(KeyError):
        factory("default")


def test_build_parser_factory_delegates_to_strategy_and_caches() -> None:
    # The parser factory must route a preset's parsing_strategy through the
    # dispatcher's for_pdf_strategy (so pymupdf/docling are honored, not the
    # global default) and cache per strategy so no backend/pool is duplicated.
    from services.workers.indexer_pool import _build_parser_factory

    calls: list[str] = []

    class _FakeDispatcher:
        def for_pdf_strategy(self, strategy: str):
            calls.append(strategy)
            return SimpleNamespace(strategy=strategy)

    factory = _build_parser_factory(_FakeDispatcher())

    first = factory("pymupdf")
    assert first.strategy == "pymupdf"
    assert factory("pymupdf") is first  # cached: built once per strategy
    assert factory("docling").strategy == "docling"
    assert calls == ["pymupdf", "docling"]  # no rebuild for the repeated strategy


def test_contextualizer_factory_reads_live_registry(tmp_path) -> None:
    # The factory holds a live reference to cfg.models.llm: a name added to the
    # registry AFTER the factory is built (mimicking the indexer's lazy DB
    # hydration) must resolve without rebuilding the factory.
    from core.config.model_endpoints import ModelEndpointConfig
    from core.llm import llm_registry
    from services.workers.indexer_pool import _build_contextualizer_factory

    class ProbeLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    llm_registry.register("live-probe-llm")(ProbeLLM)
    try:
        (tmp_path / "ctx.txt").write_text("Context prompt", encoding="utf-8")
        registry: dict = {}
        cfg = SimpleNamespace(
            models=SimpleNamespace(llm=registry),
            llm=SimpleNamespace(base_url="", model="", api_key=""),
            chunker=SimpleNamespace(contextualization_timeout=12, max_concurrent_contextualization=3),
            semaphore=SimpleNamespace(llm_semaphore=4),
            paths=SimpleNamespace(prompts_dir=str(tmp_path)),
            prompts=SimpleNamespace(chunk_contextualizer="ctx.txt"),
        )

        factory = _build_contextualizer_factory(cfg)
        with pytest.raises(KeyError):
            factory("late")

        # Hydration mutates the same dict in place (dict.clear()+update()).
        registry["late"] = ModelEndpointConfig(
            endpoint="http://late.example/v1",
            model_name="late-model",
            extra={"implementation": "live-probe-llm"},
        )

        contextualizer = factory("late")
        assert contextualizer._llm.kwargs["endpoint"] == "http://late.example/v1"
        assert contextualizer._llm.kwargs["model_name"] == "late-model"
    finally:
        llm_registry._registry.pop("live-probe-llm", None)


def test_embedder_factory_reads_live_registry() -> None:
    from core.embeddings import embedder_registry
    from services.workers.indexer_pool import _build_embedder_factory

    class ProbeEmbedder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    embedder_registry.register("live-probe-embedder")(ProbeEmbedder)
    try:
        registry: dict = {}
        cfg = SimpleNamespace(
            models=SimpleNamespace(embedder=registry),
            embedder=SimpleNamespace(base_url="", model_name="", api_key=""),
        )

        factory = _build_embedder_factory(cfg)
        assert factory is not None
        with pytest.raises(KeyError):
            factory("late")

        registry["late"] = ModelEndpointConfig(
            endpoint="http://embed.example/v1",
            model_name="embed-model",
            timeout=13,
            batch_size=7,
            extra={"implementation": "live-probe-embedder", "api_key": "embed-key", "max_model_len": 2047},
        )

        embedder = factory("late")
        assert embedder.kwargs["endpoint"] == "http://embed.example/v1"
        assert embedder.kwargs["model_name"] == "embed-model"
        assert embedder.kwargs["batch_size"] == 7
        assert embedder.kwargs["timeout"] == 13
        assert embedder.kwargs["api_key"] == "embed-key"
        assert embedder.kwargs["max_model_len"] == 2047
    finally:
        embedder_registry._registry.pop("live-probe-embedder", None)


def test_embedder_factory_backfills_max_model_len_from_settings() -> None:
    """A named endpoint whose `extra` omits max_model_len inherits it from the
    static embedder settings (an explicit per-endpoint value still wins)."""
    from core.embeddings import embedder_registry
    from services.workers.indexer_pool import _build_embedder_factory

    class ProbeEmbedder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    embedder_registry.register("backfill-probe-embedder")(ProbeEmbedder)
    try:
        registry: dict = {
            "no-extra": ModelEndpointConfig(
                endpoint="http://embed.example/v1",
                model_name="embed-model",
                extra={"implementation": "backfill-probe-embedder", "api_key": "k"},
            ),
            "explicit": ModelEndpointConfig(
                endpoint="http://embed.example/v1",
                model_name="embed-model",
                extra={"implementation": "backfill-probe-embedder", "max_model_len": 4096},
            ),
        }
        cfg = SimpleNamespace(
            models=SimpleNamespace(embedder=registry),
            embedder=SimpleNamespace(max_model_len=2047, embed_concurrency=4),
        )

        factory = _build_embedder_factory(cfg)
        backfilled = factory("no-extra")
        assert backfilled.kwargs["max_model_len"] == 2047
        assert backfilled.kwargs["embed_concurrency"] == 4
        assert factory("explicit").kwargs["max_model_len"] == 4096  # per-endpoint extra wins
    finally:
        embedder_registry._registry.pop("backfill-probe-embedder", None)


def test_embedder_factory_rebuilds_on_api_key_rotation() -> None:
    from core.embeddings import embedder_registry
    from services.workers.indexer_pool import _build_embedder_factory

    class ProbeEmbedder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    embedder_registry.register("key-probe-embedder")(ProbeEmbedder)
    try:
        registry = {
            "ep": ModelEndpointConfig(
                endpoint="http://embed.example/v1",
                model_name="embed-model",
                extra={"implementation": "key-probe-embedder", "api_key": "k1"},
            )
        }
        cfg = SimpleNamespace(
            models=SimpleNamespace(embedder=registry),
            embedder=SimpleNamespace(base_url="", model_name="", api_key=""),
        )

        factory = _build_embedder_factory(cfg)
        first = factory("ep")

        registry["ep"] = ModelEndpointConfig(
            endpoint="http://embed.example/v1",
            model_name="embed-model",
            extra={"implementation": "key-probe-embedder", "api_key": "k2"},
        )
        second = factory("ep")

        assert second is not first
        assert second.kwargs["api_key"] == "k2"
    finally:
        embedder_registry._registry.pop("key-probe-embedder", None)


def test_vlm_factory_reads_live_registry() -> None:
    from core.vlm import vlm_registry
    from services.workers.indexer_pool import _build_vlm_factory

    class ProbeVLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    vlm_registry.register("live-probe-vlm")(ProbeVLM)
    try:
        registry: dict = {}
        cfg = SimpleNamespace(
            models=SimpleNamespace(vlm=registry),
            vlm=SimpleNamespace(base_url="", model="", api_key="", timeout=60, enable_thinking=None),
        )

        factory = _build_vlm_factory(cfg)
        with pytest.raises(KeyError):
            factory("late")

        registry["late"] = ModelEndpointConfig(
            endpoint="http://vlm.example/v1",
            model_name="vlm-model",
            timeout=17,
            extra={"implementation": "live-probe-vlm", "api_key": "vlm-key", "enable_thinking": False},
        )

        vlm = factory("late")
        assert vlm.kwargs["endpoint"] == "http://vlm.example/v1"
        assert vlm.kwargs["model_name"] == "vlm-model"
        assert vlm.kwargs["timeout"] == 17
        assert vlm.kwargs["api_key"] == "vlm-key"
        assert vlm.kwargs["enable_thinking"] is False
    finally:
        vlm_registry._registry.pop("live-probe-vlm", None)


def test_vlm_factory_rebuilds_on_endpoint_edit() -> None:
    from core.vlm import vlm_registry
    from services.workers.indexer_pool import _build_vlm_factory

    class ProbeVLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    vlm_registry.register("edit-probe-vlm")(ProbeVLM)
    try:
        registry = {
            "ep": ModelEndpointConfig(
                endpoint="http://vlm-v1.example/v1",
                model_name="vlm-v1",
                extra={"implementation": "edit-probe-vlm"},
            )
        }
        cfg = SimpleNamespace(
            models=SimpleNamespace(vlm=registry),
            vlm=SimpleNamespace(base_url="", model="", api_key="", timeout=60, enable_thinking=None),
        )

        factory = _build_vlm_factory(cfg)
        first = factory("ep")

        registry["ep"] = ModelEndpointConfig(
            endpoint="http://vlm-v2.example/v1",
            model_name="vlm-v2",
            extra={"implementation": "edit-probe-vlm"},
        )
        second = factory("ep")

        assert second is not first
        assert second.kwargs["endpoint"] == "http://vlm-v2.example/v1"
        assert second.kwargs["model_name"] == "vlm-v2"
    finally:
        vlm_registry._registry.pop("edit-probe-vlm", None)


def test_required_llm_names_mirrors_pipeline_selection() -> None:
    from services.workers.indexer_pool import _required_llm_names

    assert _required_llm_names(None) == []
    # Both topic tagging and contextualization default off.
    assert _required_llm_names({}) == []
    assert _required_llm_names({"enable_topic_tagging": False}) == []
    assert _required_llm_names({"enable_topic_tagging": True}) == ["default"]
    assert _required_llm_names(
        {
            "enable_contextualization": True,
            "contextualization_llm": "ctx",
            "enable_topic_tagging": True,
            "topic_tagging_llm": "tags",
        }
    ) == ["ctx", "tags"]


def test_required_model_endpoint_names_include_embedder_vlm_and_stt() -> None:
    from services.workers.indexer_pool import _required_model_endpoint_names

    required = _required_model_endpoint_names(
        {
            "enable_image_captioning": True,
            "vlm": "vlm-fast",
            "enable_contextualization": True,
            "contextualization_llm": "ctx",
            "enable_topic_tagging": True,
            "topic_tagging_llm": "tags",
        },
        embedder_name="embed-fast",
    )

    assert required == {
        "embedder": ["embed-fast"],
        "llm": ["ctx", "tags"],
        "vlm": ["vlm-fast"],
        "stt": ["default"],
    }


def test_registry_reload_decision_guards() -> None:
    from services.workers.indexer_pool import _registry_reload_decision

    # First use always loads.
    assert _registry_reload_decision(loaded_at=None, last_miss_at=None, now=100.0, ttl=60.0, missing=False) == "initial"
    # Hit path: fresh and nothing missing → no reload, no I/O.
    assert _registry_reload_decision(loaded_at=100.0, last_miss_at=None, now=110.0, ttl=60.0, missing=False) is None
    # TTL expiry refreshes (catches edits to existing endpoints).
    assert _registry_reload_decision(loaded_at=100.0, last_miss_at=None, now=161.0, ttl=60.0, missing=False) == "ttl"
    # A missing name within the window triggers one reload...
    assert _registry_reload_decision(loaded_at=100.0, last_miss_at=None, now=110.0, ttl=60.0, missing=True) == "miss"
    # ...but is rate-limited: a still-missing name can't reload again until the
    # next window, so a deleted/typo'd name can't storm the DB.
    assert _registry_reload_decision(loaded_at=100.0, last_miss_at=105.0, now=120.0, ttl=60.0, missing=True) is None
    # A missing name takes priority over a stale registry: it must block ("miss"),
    # not fall through to a background "ttl" refresh that would skip the stage.
    assert _registry_reload_decision(loaded_at=100.0, last_miss_at=None, now=200.0, ttl=60.0, missing=True) == "miss"
    # Rate-limited miss while ALSO stale → still refresh the stale registry in the
    # background ("ttl") rather than nothing.
    assert _registry_reload_decision(loaded_at=100.0, last_miss_at=150.0, now=200.0, ttl=60.0, missing=True) == "ttl"


def test_registry_reload_decision_rate_limits_only_same_missing_signature() -> None:
    from services.workers.indexer_pool import _registry_reload_decision

    previous_missing = (("embedder", ("missing-a",)),)
    same_missing = (("embedder", ("missing-a",)),)
    different_missing = (("embedder", ("missing-b",)),)

    assert (
        _registry_reload_decision(
            loaded_at=100.0,
            last_miss_at=105.0,
            last_miss_key=previous_missing,
            missing_key=same_missing,
            now=120.0,
            ttl=60.0,
            missing=True,
        )
        is None
    )
    assert (
        _registry_reload_decision(
            loaded_at=100.0,
            last_miss_at=105.0,
            last_miss_key=previous_missing,
            missing_key=different_missing,
            now=120.0,
            ttl=60.0,
            missing=True,
        )
        == "miss"
    )


def test_reload_decision_treats_default_global_fallback_as_resolvable() -> None:
    # "default" resolves via the global cfg.llm fallback even when the registry
    # has no is_default row → it must NOT be treated as missing, otherwise we'd
    # block-reload every window forever without converging.
    import time as _time

    from services.workers.indexer_pool import IndexerWorkerActor

    actor_class = IndexerWorkerActor.__ray_metadata__.modified_class

    def _pool(has_fallback: bool):
        pool = actor_class.__new__(actor_class)
        pool._cfg = SimpleNamespace(models=SimpleNamespace(llm={}))  # registry lacks "default"
        pool._has_default_fallback = has_fallback
        pool._registry_loaded_at = _time.monotonic()  # fresh, not stale
        pool._last_miss_reload_at = None
        return pool

    # Fallback present → "default" resolvable → hit path, no reload.
    assert _pool(True)._reload_decision(["default"]) is None
    # No fallback and not in registry → genuinely missing → reload.
    assert _pool(False)._reload_decision(["default"]) == "miss"
    # A *named* endpoint (not "default") is never covered by the fallback.
    assert _pool(True)._reload_decision(["acme-llm"]) == "miss"


@pytest.mark.asyncio
async def test_ensure_registry_fresh_is_single_flight() -> None:
    from services.workers.indexer_pool import IndexerWorkerActor

    actor_class = IndexerWorkerActor.__ray_metadata__.modified_class
    pool = actor_class.__new__(actor_class)

    class Service:
        def __init__(self) -> None:
            self.calls = 0

        async def load_all(self) -> None:
            self.calls += 1
            await asyncio.sleep(0)

    pool._cfg = SimpleNamespace(models=SimpleNamespace(llm={}))
    pool._model_endpoint_service = Service()
    pool._registry_loaded_at = None
    pool._last_miss_reload_at = None
    pool._registry_lock = asyncio.Lock()
    pool._registry_reload_task = None

    await asyncio.gather(*(pool._ensure_registry_fresh([]) for _ in range(20)))

    assert pool._model_endpoint_service.calls == 1
    assert pool._registry_loaded_at is not None


@pytest.mark.asyncio
async def test_ttl_refresh_runs_in_background_without_blocking() -> None:
    # A periodic TTL refresh must not sit on a file's critical path: the current
    # registry is still valid, so _ensure_registry_fresh returns immediately and
    # the reload runs in the background.
    import time as _time

    from services.workers.indexer_pool import _MODEL_REGISTRY_TTL_SECONDS, IndexerWorkerActor

    actor_class = IndexerWorkerActor.__ray_metadata__.modified_class
    pool = actor_class.__new__(actor_class)

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowService:
        def __init__(self) -> None:
            self.calls = 0

        async def load_all(self) -> None:
            self.calls += 1
            started.set()
            await release.wait()

    pool._cfg = SimpleNamespace(models=SimpleNamespace(llm={"default": object()}))
    pool._model_endpoint_service = SlowService()
    pool._registry_loaded_at = _time.monotonic() - _MODEL_REGISTRY_TTL_SECONDS - 1  # stale → "ttl"
    pool._last_miss_reload_at = None
    pool._registry_lock = asyncio.Lock()
    pool._registry_reload_task = None

    # Returns promptly even though load_all is still blocked.
    await asyncio.wait_for(pool._ensure_registry_fresh(["default"]), timeout=0.5)
    await asyncio.wait_for(started.wait(), timeout=0.5)
    assert pool._registry_reload_task is not None and not pool._registry_reload_task.done()

    release.set()
    await pool._registry_reload_task
    assert pool._model_endpoint_service.calls == 1


def test_contextualizer_factory_rebuilds_on_endpoint_edit(tmp_path) -> None:
    # An edited endpoint (changed identity) must yield a fresh client; the cache
    # holds one entry per name (replaced, not accumulated), so it can't leak a
    # stale client per edit over the long-lived actor.
    from core.config.model_endpoints import ModelEndpointConfig
    from core.llm import llm_registry
    from services.workers.indexer_pool import _build_contextualizer_factory

    class ProbeLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    llm_registry.register("edit-probe-llm")(ProbeLLM)
    try:
        (tmp_path / "ctx.txt").write_text("Context prompt", encoding="utf-8")
        registry = {
            "ep": ModelEndpointConfig(
                endpoint="http://v1.example/v1", model_name="m1", extra={"implementation": "edit-probe-llm"}
            )
        }
        cfg = SimpleNamespace(
            models=SimpleNamespace(llm=registry),
            llm=SimpleNamespace(base_url="", model="", api_key=""),
            chunker=SimpleNamespace(contextualization_timeout=12, max_concurrent_contextualization=3),
            semaphore=SimpleNamespace(llm_semaphore=4),
            paths=SimpleNamespace(prompts_dir=str(tmp_path)),
            prompts=SimpleNamespace(chunk_contextualizer="ctx.txt"),
        )

        factory = _build_contextualizer_factory(cfg)
        first = factory("ep")
        assert factory("ep") is first  # unchanged identity → cached

        # Edit the endpoint in place (mimicking a registry reload after a change).
        registry["ep"] = ModelEndpointConfig(
            endpoint="http://v2.example/v1", model_name="m2", extra={"implementation": "edit-probe-llm"}
        )
        second = factory("ep")
        assert second is not first
        assert second._llm.kwargs["endpoint"] == "http://v2.example/v1"
    finally:
        llm_registry._registry.pop("edit-probe-llm", None)


def test_endpoint_identity_covers_full_config() -> None:
    # The cache identity must change for ANY config edit — including an
    # extra-only change like an api-key rotation (same URL + model) — so the
    # client is rebuilt after a reload, matching the API's invalidate-on-change.
    from core.config.model_endpoints import ModelEndpointConfig
    from services.workers.indexer_pool import _endpoint_identity

    base = ModelEndpointConfig(endpoint="http://e/v1", model_name="m", extra={"api_key": "k1"})
    same = ModelEndpointConfig(endpoint="http://e/v1", model_name="m", extra={"api_key": "k1"})
    rotated_key = ModelEndpointConfig(endpoint="http://e/v1", model_name="m", extra={"api_key": "k2"})

    assert _endpoint_identity(base) == _endpoint_identity(same)
    assert _endpoint_identity(base) != _endpoint_identity(rotated_key)


def test_contextualizer_factory_rebuilds_on_api_key_rotation(tmp_path) -> None:
    # Same endpoint + model, only the api_key changes → the cached client must
    # still be rebuilt (the old key would otherwise persist until restart).
    from core.config.model_endpoints import ModelEndpointConfig
    from core.llm import llm_registry
    from services.workers.indexer_pool import _build_contextualizer_factory

    class ProbeLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    llm_registry.register("key-probe-llm")(ProbeLLM)
    try:
        (tmp_path / "ctx.txt").write_text("Context prompt", encoding="utf-8")
        registry = {
            "ep": ModelEndpointConfig(
                endpoint="http://e/v1", model_name="m", extra={"implementation": "key-probe-llm", "api_key": "k1"}
            )
        }
        cfg = SimpleNamespace(
            models=SimpleNamespace(llm=registry),
            llm=SimpleNamespace(base_url="", model="", api_key=""),
            chunker=SimpleNamespace(contextualization_timeout=12, max_concurrent_contextualization=3),
            semaphore=SimpleNamespace(llm_semaphore=4),
            paths=SimpleNamespace(prompts_dir=str(tmp_path)),
            prompts=SimpleNamespace(chunk_contextualizer="ctx.txt"),
        )

        factory = _build_contextualizer_factory(cfg)
        first = factory("ep")

        registry["ep"] = ModelEndpointConfig(
            endpoint="http://e/v1", model_name="m", extra={"implementation": "key-probe-llm", "api_key": "k2"}
        )
        second = factory("ep")
        assert second is not first
        assert second._llm.kwargs["api_key"] == "k2"
    finally:
        llm_registry._registry.pop("key-probe-llm", None)


def test_global_llm_endpoint_config_carries_sampling_params() -> None:
    """The fallback LLM endpoint config must carry temperature/max_retries/
    logprobs so it behaves the same as a named endpoint (#720) — and
    ``logprobs`` must default to False (LLMParamsConfig's real default), not
    True.
    """
    from services.workers.indexer_pool import _global_llm_endpoint_config

    cfg = SimpleNamespace(
        llm=SimpleNamespace(
            base_url="http://llm.example/v1",
            model="mistral",
            api_key="llm-key",
            temperature=0.42,
            max_retries=9,
            logprobs=True,
            timeout=60,
        )
    )

    endpoint_cfg = _global_llm_endpoint_config(cfg)

    assert endpoint_cfg.extra["temperature"] == 0.42
    assert endpoint_cfg.extra["max_retries"] == 9
    assert endpoint_cfg.extra["logprobs"] is True


def test_global_llm_endpoint_config_logprobs_defaults_false() -> None:
    from services.workers.indexer_pool import _global_llm_endpoint_config

    cfg = SimpleNamespace(llm=SimpleNamespace(base_url="http://llm.example/v1", model="mistral"))

    endpoint_cfg = _global_llm_endpoint_config(cfg)

    assert endpoint_cfg.extra["logprobs"] is False


def test_global_vlm_endpoint_config_carries_sampling_params() -> None:
    """Mirrors the LLM fallback: the VLM fallback must also carry sampling
    params instead of dropping temperature/max_retries/logprobs entirely.
    """
    from services.workers.indexer_pool import _global_vlm_endpoint_config

    cfg = SimpleNamespace(
        vlm=SimpleNamespace(
            base_url="http://vlm.example/v1",
            model="pixtral",
            api_key="vlm-key",
            temperature=0.55,
            max_retries=4,
            logprobs=True,
            timeout=60,
        )
    )

    endpoint_cfg = _global_vlm_endpoint_config(cfg)

    assert endpoint_cfg.extra["temperature"] == 0.55
    assert endpoint_cfg.extra["max_retries"] == 4
    assert endpoint_cfg.extra["logprobs"] is True


def test_build_contextualizer_factory_uses_global_llm_fallback(tmp_path) -> None:
    from services.workers.indexer_pool import _build_contextualizer_factory

    (tmp_path / "chunk_contextualizer_tmpl.txt").write_text("Context prompt", encoding="utf-8")
    cfg = SimpleNamespace(
        models=SimpleNamespace(llm={}),
        llm=SimpleNamespace(
            base_url="http://llm.example/v1",
            model="mistral",
            api_key="llm-key",
            enable_thinking=False,
        ),
        chunker=SimpleNamespace(contextualization_timeout=12, max_concurrent_contextualization=3),
        semaphore=SimpleNamespace(llm_semaphore=7),
        paths=SimpleNamespace(prompts_dir=str(tmp_path)),
        prompts=SimpleNamespace(chunk_contextualizer="chunk_contextualizer_tmpl.txt"),
    )

    factory = _build_contextualizer_factory(cfg)

    contextualizer = factory("default")
    assert contextualizer is factory("default")
    assert contextualizer._system_prompt == "Context prompt"
    assert contextualizer._timeout == 12
    assert contextualizer._batch_size == 3
    assert contextualizer._llm._endpoint == "http://llm.example/v1"
    assert contextualizer._llm._model == "mistral"
    assert contextualizer._llm._api_key == "llm-key"
    assert contextualizer._llm._enable_thinking is False
    # _batch_size drives the per-document loop; _llm.chat is gated by the
    # injected cluster-wide "llmSemaphore".
    assert contextualizer._semaphore._name == "llmSemaphore"
    assert contextualizer._semaphore._max_concurrent_ops == 7


def test_build_contextualizer_factory_uses_named_llm_endpoint(tmp_path) -> None:
    from core.config.model_endpoints import ModelEndpointConfig
    from core.llm import llm_registry
    from services.workers.indexer_pool import _build_contextualizer_factory

    class FakeLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def chat(self, messages, **kwargs):
            return {"choices": [{"message": {"content": "document context"}}]}

    llm_registry.register("test-contextualizer-llm")(FakeLLM)
    try:
        (tmp_path / "chunk_contextualizer_tmpl.txt").write_text("Context prompt", encoding="utf-8")
        cfg = SimpleNamespace(
            models=SimpleNamespace(
                llm={
                    "ctx": ModelEndpointConfig(
                        endpoint="http://ctx.example/v1",
                        model_name="ctx-model",
                        timeout=45,
                        extra={"implementation": "test-contextualizer-llm", "api_key": "ctx-key", "temperature": 0.2},
                    )
                }
            ),
            llm=SimpleNamespace(base_url="http://fallback.example/v1", model="fallback", api_key="fallback-key"),
            chunker=SimpleNamespace(contextualization_timeout=12, max_concurrent_contextualization=3),
            semaphore=SimpleNamespace(llm_semaphore=7),
            paths=SimpleNamespace(prompts_dir=str(tmp_path)),
            prompts=SimpleNamespace(chunk_contextualizer="chunk_contextualizer_tmpl.txt"),
        )

        factory = _build_contextualizer_factory(cfg)

        contextualizer = factory("ctx")
        assert contextualizer is factory("ctx")
        assert contextualizer._llm.kwargs == {
            "endpoint": "http://ctx.example/v1",
            "model_name": "ctx-model",
            "timeout": 45.0,
            "api_key": "ctx-key",
            "temperature": 0.2,
        }
        assert contextualizer._semaphore._name == "llmSemaphore"
        assert contextualizer._semaphore._max_concurrent_ops == 7
    finally:
        # FakeLLM lives only for this test — drop it so the shared llm_registry
        # doesn't leak into other tests in the same process.
        llm_registry._registry.pop("test-contextualizer-llm", None)


class _FakeWorker:
    """Stand-in for an ``IndexerWorkerActor`` handle.

    ``process_file.remote(**kwargs)`` returns an ``asyncio.Future`` that
    plays the role of a Ray ``ObjectRef`` (``asyncio.gather`` accepts both),
    so tests can drive task completion deterministically.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.futures: list[asyncio.Future] = []
        self.process_file = SimpleNamespace(remote=self._remote)

    def _remote(self, **kwargs):
        fut = asyncio.get_running_loop().create_future()
        self.calls.append(kwargs)
        self.futures.append(fut)
        return fut


def _bare_pool(workers: list) -> object:
    """An ``IndexerPool`` actor instance with ``__init__`` bypassed.

    The dispatch/release logic under test lives on the actor class; we set the
    fields it touches directly so tests can inject fake workers instead of
    spawning real Ray actors.
    """
    from services.workers.indexer_pool import IndexerPool

    actor_class = IndexerPool.__ray_metadata__.modified_class
    pool = actor_class.__new__(actor_class)
    pool._workers = list(workers)
    pool._worker_names = [f"test-worker-{index}" for index in range(len(workers))]
    pool._inflight = [0] * len(workers)
    pool._accepting_tasks = True
    pool._release_tasks = set()
    pool._claim_store = None
    pool._claim_store_lock = asyncio.Lock()
    pool._namespace = "openrag"
    pool._task_state_manager = SimpleNamespace(
        accept_worker_submission=SimpleNamespace(remote=AsyncMock(return_value=True)),
        set_object_ref=SimpleNamespace(remote=AsyncMock(return_value=True)),
        finish_rejected_submission=SimpleNamespace(remote=AsyncMock(return_value=True)),
    )
    return pool


async def _settle_pool_release_tasks(pool: object, *futures: asyncio.Future[object]) -> None:
    for fut in futures:
        if not fut.done():
            fut.set_result(None)
    release_tasks = list(getattr(pool, "_release_tasks"))
    if release_tasks:
        await asyncio.gather(*release_tasks)


@pytest.mark.asyncio
async def test_claim_repo_preserves_configured_catalog_database(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.config
    import services.storage.postgres_store as postgres_store

    pool = _bare_pool([_FakeWorker()])
    rdb = SimpleNamespace(database="custom_catalog")
    cfg = SimpleNamespace(rdb=rdb, vectordb=SimpleNamespace(collection_name="ignored_collection"))
    repo = object()
    calls = []

    class Store:
        def __init__(self, config, *, run_migrations):
            self.document_repo = repo
            calls.append((config, run_migrations))

        async def initialize(self) -> None:
            calls.append("initialized")

    monkeypatch.setattr(core.config, "load_config", lambda: cfg)
    monkeypatch.setattr(postgres_store, "PostgresStore", Store)

    assert await pool._claim_document_repo() is repo
    assert calls == [(rdb, False), "initialized"]


def test_pool_requires_positive_pool_size() -> None:
    from services.workers.indexer_pool import IndexerPool

    actor_class = IndexerPool.__ray_metadata__.modified_class
    with pytest.raises(ValueError):
        actor_class(pool_size=0, max_tasks_per_worker=4)


@pytest.mark.asyncio
async def test_pool_dispatches_to_least_loaded_and_passes_ref_through() -> None:
    workers = [_FakeWorker(), _FakeWorker()]
    pool = _bare_pool(workers)

    ref0 = await pool.submit(task_id="a")  # tie -> worker 0
    await pool.submit(task_id="b")  # worker 0 busy -> worker 1
    await pool.submit(task_id="c")  # tie (1 each) -> worker 0

    assert len(workers[0].calls) == 2
    assert len(workers[1].calls) == 1
    # The ObjectRef is passed through wrapped in a one-element list (the
    # dispatcher unwraps it; the wrapper stops Ray auto-dereferencing the ref).
    assert ref0 == [workers[0].futures[0]]
    assert pool._task_state_manager.accept_worker_submission.remote.await_count == 3
    assert pool._task_state_manager.set_object_ref.remote.await_count == 3
    await _settle_pool_release_tasks(
        pool,
        workers[0].futures[0],
        workers[1].futures[0],
        workers[0].futures[1],
    )


@pytest.mark.asyncio
async def test_pool_settles_worker_when_task_handoff_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.indexer_pool as module

    worker = _FakeWorker()
    pool = _bare_pool([worker])
    pool._task_state_manager.accept_worker_submission.remote.return_value = False
    cancellation_requested = asyncio.Event()
    monkeypatch.setattr(
        module.ray,
        "cancel",
        MagicMock(side_effect=lambda *_args, **_kwargs: cancellation_requested.set()),
    )

    submission = asyncio.create_task(pool.submit(task_id="task-1"))
    await asyncio.wait_for(cancellation_requested.wait(), timeout=1)
    assert len(worker.calls) == 1
    worker.futures[0].set_result(None)

    with pytest.raises(RuntimeError, match="cancelled before the pool accepted"):
        await submission
    assert pool._inflight == [0]
    pool._task_state_manager.finish_rejected_submission.remote.assert_awaited_once_with("task-1")


@pytest.mark.asyncio
async def test_pool_drain_rejects_new_work_and_reports_accepted_work(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.indexer_pool as module

    worker = _FakeWorker()
    pool = _bare_pool([worker])
    repo = SimpleNamespace(release_content_sha256_claim=AsyncMock())
    pool._claim_store = SimpleNamespace(document_repo=repo)

    await pool.submit(task_id="accepted-before-drain")

    assert await pool.begin_drain() == {
        "protocol_version": "v5",
        "accepting_tasks": False,
        "inflight_jobs": 1,
        "worker_names": ["test-worker-0"],
    }
    recovered_task_state_manager = SimpleNamespace(
        finish_rejected_submission=SimpleNamespace(remote=AsyncMock(return_value=True))
    )
    pool._task_state_manager = None
    get_actor = MagicMock(return_value=recovered_task_state_manager)
    monkeypatch.setattr(module.ray, "get_actor", get_actor)
    with pytest.raises(RuntimeError, match="draining"):
        await pool.submit(
            task_id="rejected-after-drain",
            partition="tenant-a",
            metadata={
                "file_id": "file-1",
                "content_sha256": "abc123",
                CONTENT_CLAIM_TOKEN_METADATA_KEY: "attempt-1",
            },
        )
    assert len(worker.calls) == 1
    get_actor.assert_called_once_with("TaskStateManager", namespace="openrag")
    recovered_task_state_manager.finish_rejected_submission.remote.assert_awaited_once_with("rejected-after-drain")
    repo.release_content_sha256_claim.assert_awaited_once_with(
        file_id="file-1",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-1",
    )

    await _settle_pool_release_tasks(pool, worker.futures[0])
    assert await pool.status() == {
        "protocol_version": "v5",
        "accepting_tasks": False,
        "inflight_jobs": 0,
        "worker_names": ["test-worker-0"],
    }


@pytest.mark.asyncio
async def test_pool_abort_drain_restores_acceptance() -> None:
    worker = _FakeWorker()
    pool = _bare_pool([worker])

    await pool.begin_drain()
    with pytest.raises(RuntimeError, match="draining"):
        await pool.submit(task_id="rejected-while-draining")

    assert await pool.abort_drain() == {
        "protocol_version": "v5",
        "accepting_tasks": True,
        "inflight_jobs": 0,
        "worker_names": ["test-worker-0"],
    }

    await pool.submit(task_id="accepted-after-abort")
    assert len(worker.calls) == 1
    await _settle_pool_release_tasks(pool, worker.futures[0])


@pytest.mark.asyncio
async def test_pool_reports_current_protocol_version() -> None:
    pool = _bare_pool([_FakeWorker()])

    assert await pool.protocol_version() == "v5"


@pytest.mark.asyncio
async def test_pool_cancels_worker_when_ref_registration_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.workers.indexer_pool as module

    worker = _FakeWorker()
    pool = _bare_pool([worker])
    pool._task_state_manager.set_object_ref.remote.return_value = False
    cancellation_requested = asyncio.Event()
    cancel = MagicMock(side_effect=lambda *_args, **_kwargs: cancellation_requested.set())
    monkeypatch.setattr(module.ray, "cancel", cancel)

    submission = asyncio.create_task(pool.submit(task_id="task-1"))
    await asyncio.wait_for(cancellation_requested.wait(), timeout=1)

    cancel.assert_called_once_with(worker.futures[0], recursive=True)
    assert submission.done() is False

    worker.futures[0].set_result(None)
    with pytest.raises(RuntimeError, match="cancelled before worker ref registration"):
        await submission
    pool._task_state_manager.finish_rejected_submission.remote.assert_awaited_once_with("task-1")
    await _settle_pool_release_tasks(pool)


@pytest.mark.asyncio
async def test_pool_waits_for_worker_when_ref_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.workers.indexer_pool as module

    worker = _FakeWorker()
    pool = _bare_pool([worker])
    pool._task_state_manager.set_object_ref.remote.side_effect = RuntimeError("task state unavailable")
    cancellation_requested = asyncio.Event()
    monkeypatch.setattr(
        module.ray,
        "cancel",
        MagicMock(side_effect=lambda *_args, **_kwargs: cancellation_requested.set()),
    )

    submission = asyncio.create_task(pool.submit(task_id="task-1"))
    await asyncio.wait_for(cancellation_requested.wait(), timeout=1)
    assert submission.done() is False

    worker.futures[0].set_result(None)
    with pytest.raises(RuntimeError, match="task state unavailable"):
        await submission
    pool._task_state_manager.finish_rejected_submission.remote.assert_awaited_once_with("task-1")
    await _settle_pool_release_tasks(pool)


@pytest.mark.asyncio
async def test_rejected_worker_settlement_survives_submit_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.workers.indexer_pool as module

    worker = _FakeWorker()
    pool = _bare_pool([worker])
    pool._task_state_manager.set_object_ref.remote.return_value = False
    cancellation_requested = asyncio.Event()
    monkeypatch.setattr(
        module.ray,
        "cancel",
        MagicMock(side_effect=lambda *_args, **_kwargs: cancellation_requested.set()),
    )

    submission = asyncio.create_task(pool.submit(task_id="task-1"))
    await asyncio.wait_for(cancellation_requested.wait(), timeout=1)
    submission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submission

    assert worker.futures[0].done() is False
    await _settle_pool_release_tasks(pool, worker.futures[0])
    pool._task_state_manager.finish_rejected_submission.remote.assert_awaited_once_with("task-1")
    assert pool._inflight == [0]


@pytest.mark.asyncio
async def test_pool_releases_inflight_when_task_settles() -> None:
    workers = [_FakeWorker(), _FakeWorker()]
    pool = _bare_pool(workers)

    await pool.submit(task_id="a")  # worker 0
    await pool.submit(task_id="b")  # worker 1
    assert pool._inflight == [1, 1]

    # One success, one failure — both must decrement the in-flight count.
    workers[0].futures[0].set_result({"ok": True})
    workers[1].futures[0].set_exception(RuntimeError("boom"))

    for _ in range(20):
        await asyncio.sleep(0)
        if pool._inflight == [0, 0]:
            break
    assert pool._inflight == [0, 0]

    # Freed workers are eligible again on the next dispatch.
    await pool.submit(task_id="c")
    assert pool._inflight[0] == 1
    await _settle_pool_release_tasks(pool, workers[0].futures[1])


@pytest.mark.asyncio
async def test_pool_rolls_back_inflight_when_submission_raises() -> None:
    # If process_file.remote raises (e.g. unserializable args or a dead actor),
    # the in-flight count must be rolled back so the worker isn't seen as busy.
    class _RaisingWorker:
        def __init__(self) -> None:
            def _boom(**_kwargs):
                raise RuntimeError("remote submission failed")

            self.process_file = SimpleNamespace(remote=_boom)

    pool = _bare_pool([_RaisingWorker()])

    with pytest.raises(RuntimeError, match="remote submission failed"):
        await pool.submit(task_id="a")

    assert pool._inflight == [0]
    pool._task_state_manager.finish_rejected_submission.remote.assert_awaited_once_with("a")


@pytest.mark.asyncio
async def test_pool_renews_content_claim_while_task_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.indexer_pool as module

    worker = _FakeWorker()
    pool = _bare_pool([worker])
    renewed = asyncio.Event()

    async def renew(**_kwargs):
        renewed.set()
        return True

    repo = SimpleNamespace(
        renew_content_sha256_claim=AsyncMock(side_effect=renew),
        release_content_sha256_claim=AsyncMock(),
    )
    pool._claim_store = SimpleNamespace(document_repo=repo)
    pool._claim_store_lock = asyncio.Lock()
    monkeypatch.setattr(module, "_CONTENT_CLAIM_RENEW_INTERVAL_SECONDS", 0.001)

    await pool.submit(
        task_id="task-1",
        partition="tenant-a",
        metadata={
            "file_id": "file-1",
            "content_sha256": "abc123",
            CONTENT_CLAIM_TOKEN_METADATA_KEY: "attempt-1",
        },
    )
    await asyncio.wait_for(renewed.wait(), timeout=1)
    await _settle_pool_release_tasks(pool, worker.futures[0])

    repo.renew_content_sha256_claim.assert_awaited()
    assert repo.renew_content_sha256_claim.await_args.kwargs == {
        "file_id": "file-1",
        "partition": "tenant-a",
        "content_sha256": "abc123",
        "claim_token": "attempt-1",
    }
    repo.release_content_sha256_claim.assert_awaited_once_with(
        file_id="file-1",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-1",
    )


@pytest.mark.asyncio
async def test_pool_keeps_content_claim_until_cancelled_task_settles() -> None:
    worker = _FakeWorker()
    pool = _bare_pool([worker])
    repo = SimpleNamespace(
        renew_content_sha256_claim=AsyncMock(return_value=True),
        release_content_sha256_claim=AsyncMock(),
    )
    pool._claim_store = SimpleNamespace(document_repo=repo)

    await pool.submit(
        task_id="task-1",
        partition="tenant-a",
        metadata={
            "file_id": "file-1",
            "content_sha256": "abc123",
            CONTENT_CLAIM_TOKEN_METADATA_KEY: "attempt-1",
        },
    )

    worker.futures[0].cancel()
    assert repo.release_content_sha256_claim.await_count == 0
    await _settle_pool_release_tasks(pool)

    repo.release_content_sha256_claim.assert_awaited_once_with(
        file_id="file-1",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-1",
    )


def test_indexer_pool_wires_contextualizer_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.config
    import core.embeddings
    import services.storage.milvus_store as milvus_store
    import services.storage.postgres_store as postgres_store
    import services.workers.indexer_pool as module
    import services.workers.parsers.parser_dispatcher as parser_dispatcher
    import services.workers.pipeline_builder as pipeline_builder

    captured = {}
    contextualizer_factory = object()
    topic_tagger_factory = object()
    vlm_factory = object()

    class RDBConfig:
        database = "custom_catalog"

        def model_copy(self, *, update):
            return SimpleNamespace(**update)

    cfg = SimpleNamespace(
        embedder=SimpleNamespace(
            base_url="http://embedder/v1",
            model_name="embed-model",
            api_key="embed-key",
            max_model_len=2048,
            timeout=30,
            batch_size=32,
            embed_concurrency=2,
        ),
        loader=SimpleNamespace(parse_timeout=3600, save_uploaded_files=True),
        vectordb=SimpleNamespace(collection_name="vdb_test"),
        rdb=RDBConfig(),
    )

    class Store:
        document_repo = object()
        topic_tag_repo = object()

    class Worker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def fake_build_pipeline(**kwargs):
        captured.update(kwargs)
        return object()

    def fake_postgres_store(config, *, run_migrations):
        captured["catalog_config"] = config
        captured["catalog_run_migrations"] = run_migrations
        return Store()

    monkeypatch.setattr(core.config, "load_config", lambda: cfg)
    monkeypatch.setattr(module, "_build_chunker", lambda _cfg, _window=None: object())
    monkeypatch.setattr(module, "_build_embedder_factory", lambda _cfg: object())
    monkeypatch.setattr(module, "_build_vlm_factory", lambda _cfg: vlm_factory)
    monkeypatch.setattr(module, "_build_contextualizer_factory", lambda _cfg: contextualizer_factory)
    monkeypatch.setattr(module, "_build_topic_tagger_factory", lambda _cfg: topic_tagger_factory)
    monkeypatch.setattr(core.embeddings.embedder_registry, "create", lambda *args, **kwargs: object())
    monkeypatch.setattr(milvus_store, "MilvusVectorStore", lambda _cfg: object())
    monkeypatch.setattr(postgres_store, "PostgresStore", fake_postgres_store)
    monkeypatch.setattr(parser_dispatcher, "build_parser_dispatcher", lambda _cfg, **_kwargs: object())
    monkeypatch.setattr(parser_dispatcher, "build_caption_vlm", lambda _cfg: object())
    monkeypatch.setattr(pipeline_builder, "build_indexing_pipeline", fake_build_pipeline)
    actor_calls = []

    def fake_get_actor(*args, **kwargs):
        actor_calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(module.ray, "get_actor", fake_get_actor)
    monkeypatch.setattr(module, "IndexerWorker", Worker)

    actor_class = module.IndexerWorkerActor.__ray_metadata__.modified_class
    actor_class()

    assert actor_calls
    assert actor_calls[0][0][0] == "TaskStateManager"
    assert actor_calls[0][1].get("namespace") == "openrag"
    assert captured["contextualizer_factory"] is contextualizer_factory
    assert captured["topic_tagger_factory"] is topic_tagger_factory
    assert captured["vlm_factory"] is vlm_factory
    assert captured["catalog_config"] is cfg.rdb
    assert captured["catalog_config"].database == "custom_catalog"
    assert captured["catalog_run_migrations"] is False


def test_indexer_pool_loads_caption_prompt_without_global_vlm_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A preset can caption through a *named* VLM endpoint (resolved per-row via
    # vlm_factory) even when no global default VLM is configured. The caption
    # prompt must still be loaded in that case, or that preset silently falls
    # back to the VLM client's bare default (#692 regression for named VLMs).
    import core.config
    import core.embeddings
    import services.storage.milvus_store as milvus_store
    import services.storage.postgres_store as postgres_store
    import services.workers.indexer_pool as module
    import services.workers.parsers.parser_dispatcher as parser_dispatcher
    import services.workers.pipeline_builder as pipeline_builder

    captured = {}

    class RDBConfig:
        database = None

        def model_copy(self, *, update):
            return SimpleNamespace(**update)

    cfg = SimpleNamespace(
        embedder=SimpleNamespace(
            base_url="http://embedder/v1",
            model_name="embed-model",
            api_key="embed-key",
            max_model_len=2048,
            timeout=30,
            batch_size=32,
            embed_concurrency=2,
        ),
        loader=SimpleNamespace(parse_timeout=3600, save_uploaded_files=True),
        vectordb=SimpleNamespace(collection_name="vdb_test"),
        rdb=RDBConfig(),
    )

    class Store:
        document_repo = object()
        topic_tag_repo = object()

    class Worker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def fake_build_pipeline(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(core.config, "load_config", lambda: cfg)
    monkeypatch.setattr(module, "_build_chunker", lambda _cfg, _window=None: object())
    monkeypatch.setattr(module, "_build_embedder_factory", lambda _cfg: object())
    monkeypatch.setattr(module, "_build_vlm_factory", lambda _cfg: object())
    monkeypatch.setattr(module, "_build_contextualizer_factory", lambda _cfg: object())
    monkeypatch.setattr(module, "_build_topic_tagger_factory", lambda _cfg: object())
    monkeypatch.setattr(core.embeddings.embedder_registry, "create", lambda *args, **kwargs: object())
    monkeypatch.setattr(milvus_store, "MilvusVectorStore", lambda _cfg: object())
    monkeypatch.setattr(postgres_store, "PostgresStore", lambda *args, **kwargs: Store())
    monkeypatch.setattr(parser_dispatcher, "build_parser_dispatcher", lambda _cfg, **_kwargs: object())
    # No global default VLM endpoint configured.
    monkeypatch.setattr(parser_dispatcher, "build_caption_vlm", lambda _cfg: None)
    monkeypatch.setattr(parser_dispatcher, "load_caption_prompt", lambda _cfg: "TEMPLATE TEXT")
    monkeypatch.setattr(pipeline_builder, "build_indexing_pipeline", fake_build_pipeline)
    monkeypatch.setattr(module.ray, "get_actor", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "IndexerWorker", Worker)

    actor_class = module.IndexerWorkerActor.__ray_metadata__.modified_class
    actor_class()

    assert captured["vlm"] is None
    assert captured["caption_prompt"] == "TEMPLATE TEXT"


# ---------------------------------------------------------------------------
# Tests — IndexerWorkerActor.process_file upload cleanup (SAVE_UPLOADED_FILES)
#
# The actor owns raw-upload disposal (not the inner IndexerWorker) so cleanup
# also covers failures that never reach the worker — catalog/registry init or
# the SERIALIZING state update.
# ---------------------------------------------------------------------------


class _RecordingWorker:
    """Stand-in for the inner IndexerWorker: counts calls, optionally raises."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls = 0
        self.last_kwargs = None

    async def process_file(self, **kwargs) -> dict:
        self.calls += 1
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        return {"stored_count": 1, "stage": "stored"}


def _AsyncReturn(value):
    """A stub coroutine function returning *value* for any arguments."""

    async def _call(*_a, **_k):
        return value

    return _call


def _bare_worker_actor(*, save_uploaded_files: bool, worker: _RecordingWorker):
    """Bare IndexerWorkerActor with only the attributes process_file touches."""
    from services.workers.indexer_pool import IndexerWorkerActor

    actor_class = IndexerWorkerActor.__ray_metadata__.modified_class
    actor = actor_class.__new__(actor_class)

    async def _noop(*_a, **_k):
        return None

    actor._ensure_catalog = _noop
    actor._ensure_registry_fresh = _noop
    actor._worker = worker
    actor._catalog_store = SimpleNamespace(
        workspace_repo=SimpleNamespace(),
        document_repo=SimpleNamespace(release_content_sha256_claim=AsyncMock()),
    )
    actor._save_uploaded_files = save_uploaded_files
    actor._logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    # These build the actor with __new__, so __init__ never runs. Captioning is
    # enabled by default, so ingest now resolves its prompt even for a config
    # that omits the flag — stub the service these tests don't exercise.
    actor._prompt_service = SimpleNamespace(
        resolve_prompt=_AsyncReturn("prompt"),
    )
    return actor


@pytest.mark.asyncio
async def test_actor_resolves_the_current_global_asr_prompt() -> None:
    actor = _bare_worker_actor(save_uploaded_files=True, worker=_RecordingWorker())
    resolve_prompt = AsyncMock(return_value="prompt")
    actor._prompt_service = SimpleNamespace(resolve_prompt=resolve_prompt)

    assert await actor._resolve_transcription_prompt() == "prompt"
    resolve_prompt.assert_awaited_once_with("asr_transcription")


@pytest.mark.asyncio
async def test_actor_uses_native_asr_prompt_when_resolution_fails() -> None:
    actor = _bare_worker_actor(save_uploaded_files=True, worker=_RecordingWorker())
    resolve_prompt = AsyncMock(side_effect=RuntimeError("database unavailable"))
    actor._prompt_service = SimpleNamespace(resolve_prompt=resolve_prompt)

    assert await actor._resolve_transcription_prompt() is None
    resolve_prompt.assert_awaited_once_with("asr_transcription")


@pytest.mark.asyncio
async def test_actor_keeps_upload_by_default(tmp_path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"x")
    actor = _bare_worker_actor(save_uploaded_files=True, worker=_RecordingWorker())

    await actor.process_file(task_id="t", path=str(path), metadata={"file_id": "f"}, partition="p")

    # Default: the raw upload stays on disk so the source-download route can
    # serve it back for Chainlit source viewing.
    assert path.exists()


@pytest.mark.asyncio
async def test_actor_releases_content_claim_after_indexing(tmp_path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"x")
    worker = _RecordingWorker()
    actor = _bare_worker_actor(save_uploaded_files=True, worker=worker)

    await actor.process_file(
        task_id="t",
        path=str(path),
        metadata={
            "file_id": "f",
            "content_sha256": "abc123",
            CONTENT_CLAIM_TOKEN_METADATA_KEY: "attempt-1",
        },
        partition="p",
    )

    actor._catalog_store.document_repo.release_content_sha256_claim.assert_awaited_once_with(
        file_id="f",
        partition="p",
        content_sha256="abc123",
        claim_token="attempt-1",
    )
    assert CONTENT_CLAIM_TOKEN_METADATA_KEY not in worker.last_kwargs["metadata"]


@pytest.mark.asyncio
async def test_actor_purges_upload_when_disabled(tmp_path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"x")
    actor = _bare_worker_actor(save_uploaded_files=False, worker=_RecordingWorker())

    await actor.process_file(task_id="t", path=str(path), metadata={"file_id": "f"}, partition="p")

    # save_uploaded_files=False (client manages its own files): purged on success.
    assert not path.exists()


@pytest.mark.asyncio
async def test_actor_purges_upload_on_worker_failure(tmp_path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"x")
    actor = _bare_worker_actor(save_uploaded_files=False, worker=_RecordingWorker(error=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await actor.process_file(task_id="t", path=str(path), metadata={"file_id": "f"}, partition="p")

    # The finally runs on failure too — don't leave the client's file behind.
    assert not path.exists()


@pytest.mark.asyncio
async def test_actor_purges_upload_on_pre_worker_failure(tmp_path) -> None:
    # The gap the old worker-level finally missed: catalog/registry init (or the
    # SERIALIZING state update) fails *before* the worker runs. The upload must
    # still be purged, and the worker must never be entered.
    path = tmp_path / "doc.txt"
    path.write_bytes(b"x")
    worker = _RecordingWorker()
    actor = _bare_worker_actor(save_uploaded_files=False, worker=worker)

    async def _boom(*_a, **_k):
        raise RuntimeError("pg down")

    actor._ensure_catalog = _boom

    with pytest.raises(RuntimeError, match="pg down"):
        await actor.process_file(task_id="t", path=str(path), metadata={"file_id": "f"}, partition="p")

    assert not path.exists()
    assert worker.calls == 0


@pytest.mark.asyncio
async def test_actor_keeps_upload_on_pre_worker_failure_when_saving(tmp_path) -> None:
    # Mirror image: with saving on, a pre-worker failure must not delete the file.
    path = tmp_path / "doc.txt"
    path.write_bytes(b"x")
    actor = _bare_worker_actor(save_uploaded_files=True, worker=_RecordingWorker())

    async def _boom(*_a, **_k):
        raise RuntimeError("pg down")

    actor._ensure_catalog = _boom

    with pytest.raises(RuntimeError, match="pg down"):
        await actor.process_file(task_id="t", path=str(path), metadata={"file_id": "f"}, partition="p")

    assert path.exists()
