"""Contract tests for the Compressor base guarantees."""

import asyncio

import pytest
from core.compression import Compressor, NoopCompressor, compressor_registry
from core.models.compression import CompressionOptions

OPTIONS = CompressionOptions(min_chars=0, timeout_s=1.0)


class _Fake(Compressor):
    name = "fake"

    def __init__(self, behaviour):
        self._behaviour = behaviour

    async def _compress(self, texts, *, options):
        return await self._behaviour(texts)


def _fake(fn):
    async def behaviour(texts):
        return fn(texts)

    return _Fake(behaviour)


async def test_noop_returns_input_unchanged():
    result = await NoopCompressor().compress(["a", "b"], options=OPTIONS)
    assert result.texts == ["a", "b"]
    assert result.degraded is False


async def test_empty_input_short_circuits():
    result = await NoopCompressor().compress([], options=OPTIONS)
    assert result.texts == []


async def test_order_and_count_are_preserved():
    compressor = _fake(lambda texts: [t[:2] for t in texts])
    result = await compressor.compress(["aaaa", "bbbb", "cccc"], options=OPTIONS)
    assert result.texts == ["aa", "bb", "cc"]


async def test_cardinality_mismatch_falls_back_to_originals():
    compressor = _fake(lambda texts: texts[:1])
    result = await compressor.compress(["aaaa", "bbbb"], options=OPTIONS)
    assert result.texts == ["aaaa", "bbbb"]
    assert result.degraded is True
    assert result.detail == "cardinality mismatch"


async def test_inflated_text_is_reverted_per_entry():
    compressor = _fake(lambda texts: ["x" * 100, "yy"])
    result = await compressor.compress(["short", "original"], options=OPTIONS)
    assert result.texts == ["short", "yy"]


async def test_backend_failure_returns_originals():
    def boom(texts):
        raise RuntimeError("backend down")

    result = await _fake(boom).compress(["a"], options=OPTIONS)
    assert result.texts == ["a"]
    assert result.degraded is True
    assert "backend down" in result.detail


async def test_timeout_returns_originals():
    async def slow(texts):
        await asyncio.sleep(1)
        return texts

    result = await _Fake(slow).compress(["a"], options=CompressionOptions(min_chars=0, timeout_s=0.01))
    assert result.texts == ["a"]
    assert result.detail == "timeout"


async def test_cancellation_propagates():
    async def hang(texts):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _Fake(hang).compress(["a"], options=OPTIONS)


async def test_ratio_reports_characters_removed():
    result = await _fake(lambda texts: ["ab"]).compress(["abcdefgh"], options=OPTIONS)
    assert result.ratio == pytest.approx(0.75)


def test_noop_is_registered():
    assert "noop" in compressor_registry.list_registered()
