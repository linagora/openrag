from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _NativeChunker:
    def chunk(self, document, partition: str = "default"):
        return []


class _BrokenChunker:
    pass


class _NonCallableChunker:
    chunk = None


def test_build_chunker_returns_native_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.chunking.factory as factory
    from services.workers.indexer_pool import _build_chunker

    native = _NativeChunker()
    monkeypatch.setattr(factory, "create_chunker", lambda _cfg: native)

    assert _build_chunker(object()) is native


def test_build_chunker_rejects_invalid_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.chunking.factory as factory
    from services.workers.indexer_pool import _build_chunker

    monkeypatch.setattr(factory, "create_chunker", lambda _cfg: _BrokenChunker())

    with pytest.raises(TypeError, match="chunk"):
        _build_chunker(object())


def test_build_chunker_rejects_non_callable_chunk_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.chunking.factory as factory
    from services.workers.indexer_pool import _build_chunker

    monkeypatch.setattr(factory, "create_chunker", lambda _cfg: _NonCallableChunker())

    with pytest.raises(TypeError, match="chunk"):
        _build_chunker(object())


@pytest.mark.asyncio
async def test_catalog_initialization_is_single_flight() -> None:
    from services.workers.indexer_pool import IndexerPool

    actor_class = IndexerPool.__ray_metadata__.modified_class
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


def test_build_indexer_pool_uses_detached_actor_with_configured_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.config
    import services.workers.indexer_pool as module

    calls = {}

    class Options:
        def remote(self):
            return "actor"

    def fake_options(**kwargs):
        calls.update(kwargs)
        return Options()

    cfg = SimpleNamespace(ray=SimpleNamespace(max_tasks_per_worker=4))
    monkeypatch.setattr(core.config, "load_config", lambda: cfg)
    monkeypatch.setattr(module.IndexerPool, "options", fake_options)

    assert module.build_indexer_pool() == "actor"
    assert calls["lifetime"] == "detached"
    assert calls["max_concurrency"] == 4


def test_build_contextualizer_factory_returns_none_without_llm_config(tmp_path) -> None:
    from services.workers.indexer_pool import _build_contextualizer_factory

    cfg = SimpleNamespace(
        models=SimpleNamespace(llm={}),
        llm=SimpleNamespace(base_url="", model="", api_key=""),
        chunker=SimpleNamespace(contextualization_timeout=12, max_concurrent_contextualization=3),
        paths=SimpleNamespace(prompts_dir=str(tmp_path)),
        prompts=SimpleNamespace(chunk_contextualizer="chunk_contextualizer_tmpl.txt"),
    )

    assert _build_contextualizer_factory(cfg) is None


def test_build_contextualizer_factory_uses_global_llm_fallback(tmp_path) -> None:
    from services.workers.indexer_pool import _build_contextualizer_factory

    (tmp_path / "chunk_contextualizer_tmpl.txt").write_text("Context prompt", encoding="utf-8")
    cfg = SimpleNamespace(
        models=SimpleNamespace(llm={}),
        llm=SimpleNamespace(base_url="http://llm.example/v1", model="mistral", api_key="llm-key"),
        chunker=SimpleNamespace(contextualization_timeout=12, max_concurrent_contextualization=3),
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


def test_build_contextualizer_factory_uses_named_llm_endpoint(tmp_path) -> None:
    from core.config.model_endpoints import ModelEndpointConfig
    from core.llm import llm_registry
    from services.workers.indexer_pool import _build_contextualizer_factory

    class FakeLLM:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.instances.append(self)

        async def chat(self, messages, **kwargs):
            return {"choices": [{"message": {"content": "document context"}}]}

    llm_registry.register("test-contextualizer-llm")(FakeLLM)
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


def test_indexer_pool_wires_contextualizer_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.config
    import core.embeddings
    import services.storage.milvus_store as milvus_store
    import services.storage.postgres_store as postgres_store
    import services.workers.indexer_pool as module
    import services.workers.parsers.doc_serializer_bridge as parser_bridge
    import services.workers.pipeline_builder as pipeline_builder

    captured = {}
    contextualizer_factory = object()

    class RDBConfig:
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
        vectordb=SimpleNamespace(collection_name="vdb_test"),
        rdb=RDBConfig(),
    )

    class Store:
        document_repo = object()

    class Worker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def fake_build_pipeline(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(core.config, "load_config", lambda: cfg)
    monkeypatch.setattr(module, "_build_chunker", lambda _cfg: object())
    monkeypatch.setattr(module, "_build_embedder_factory", lambda _cfg: object())
    monkeypatch.setattr(module, "_build_contextualizer_factory", lambda _cfg: contextualizer_factory)
    monkeypatch.setattr(core.embeddings.embedder_registry, "create", lambda *args, **kwargs: object())
    monkeypatch.setattr(milvus_store, "MilvusVectorStore", lambda _cfg: object())
    monkeypatch.setattr(postgres_store, "PostgresStore", lambda *args, **kwargs: Store())
    monkeypatch.setattr(parser_bridge, "DocSerializerBridgeParser", lambda **kwargs: object())
    monkeypatch.setattr(pipeline_builder, "build_indexing_pipeline", fake_build_pipeline)
    monkeypatch.setattr(module.ray, "get_actor", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "IndexerWorker", Worker)

    actor_class = module.IndexerPool.__ray_metadata__.modified_class
    actor_class()

    assert captured["contextualizer_factory"] is contextualizer_factory
