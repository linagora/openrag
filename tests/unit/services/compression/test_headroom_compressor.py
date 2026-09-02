"""Tests for the Headroom adapter, driven by a stub headroom module."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import pytest
from core.models.compression import CompressionOptions

OPTIONS = CompressionOptions(min_chars=0, timeout_s=5.0)


@dataclass
class _StubResult:
    messages: list[dict]


@dataclass
class _StubHeadroom:
    """Records calls and returns whatever ``responder`` produces."""

    responder: object = None
    calls: list[dict] = field(default_factory=list)

    def compress(self, messages, model, config):
        self.calls.append({"messages": messages, "model": model, "config": config})
        if self.responder is None:
            return _StubResult([{"role": "user", "content": m["content"][:2]} for m in messages])
        return self.responder(messages)


@pytest.fixture
def stub(monkeypatch):
    stub = _StubHeadroom()

    class CompressConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    module = types.ModuleType("headroom")
    module.compress = stub.compress
    module.CompressConfig = CompressConfig
    monkeypatch.setitem(sys.modules, "headroom", module)
    return stub


def _compressor():
    from services.compression.headroom_compressor import HeadroomCompressor

    return HeadroomCompressor()


async def test_compresses_each_text(stub):
    result = await _compressor().compress(["aaaa", "bbbb"], options=OPTIONS)
    assert result.texts == ["aa", "bb"]
    assert result.backend == "headroom"


async def test_texts_below_min_chars_are_skipped(stub):
    options = CompressionOptions(min_chars=10, timeout_s=5.0)
    result = await _compressor().compress(["short", "a longer piece of text"], options=options)
    assert result.texts[0] == "short"
    assert [m["content"] for m in stub.calls[0]["messages"]] == ["a longer piece of text"]


async def test_all_texts_below_min_chars_skips_the_backend(stub):
    result = await _compressor().compress(["a", "b"], options=CompressionOptions(min_chars=100, timeout_s=5.0))
    assert result.texts == ["a", "b"]
    assert stub.calls == []


async def test_message_count_mismatch_returns_originals(stub):
    stub.responder = lambda messages: _StubResult(messages[:1])
    result = await _compressor().compress(["aaaa", "bbbb"], options=OPTIONS)
    assert result.texts == ["aaaa", "bbbb"]


async def test_non_string_content_keeps_the_original(stub):
    stub.responder = lambda messages: _StubResult([{"role": "user", "content": [{"type": "text"}]}])
    result = await _compressor().compress(["aaaa"], options=OPTIONS)
    assert result.texts == ["aaaa"]


async def test_backend_exception_is_contained(stub):
    def boom(messages):
        raise RuntimeError("model missing")

    stub.responder = boom
    result = await _compressor().compress(["aaaa"], options=OPTIONS)
    assert result.texts == ["aaaa"]
    assert result.degraded is True


async def test_missing_dependency_raises_at_construction(monkeypatch):
    monkeypatch.setitem(sys.modules, "headroom", None)
    from services.compression.headroom_compressor import HeadroomCompressor

    with pytest.raises(RuntimeError, match="headroom-ai is not installed"):
        HeadroomCompressor()
