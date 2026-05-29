from __future__ import annotations

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
