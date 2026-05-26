from __future__ import annotations

import pytest


class _NativeChunker:
    def chunk(self, document, partition: str = "default"):
        return []


class _LegacyChunker:
    def __init__(self) -> None:
        self._core_splitter = _NativeChunker()


class _BrokenLegacyChunker:
    pass


def test_build_chunker_returns_native_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    from components.indexer.chunker.chunker import ChunkerFactory
    from services.workers.indexer_pool import _build_chunker

    native = _NativeChunker()
    monkeypatch.setattr(ChunkerFactory, "create_chunker", staticmethod(lambda _cfg: native))

    assert _build_chunker(object()) is native


def test_build_chunker_unwraps_legacy_core_splitter(monkeypatch: pytest.MonkeyPatch) -> None:
    from components.indexer.chunker.chunker import ChunkerFactory
    from services.workers.indexer_pool import _build_chunker

    legacy = _LegacyChunker()
    monkeypatch.setattr(ChunkerFactory, "create_chunker", staticmethod(lambda _cfg: legacy))

    assert _build_chunker(object()) is legacy._core_splitter


def test_build_chunker_rejects_invalid_legacy_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    from components.indexer.chunker.chunker import ChunkerFactory
    from services.workers.indexer_pool import _build_chunker

    monkeypatch.setattr(ChunkerFactory, "create_chunker", staticmethod(lambda _cfg: _BrokenLegacyChunker()))

    with pytest.raises(TypeError, match="chunk"):
        _build_chunker(object())
