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
