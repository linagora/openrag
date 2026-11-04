"""Tests for sharing query embeddings within a retrieval."""

import asyncio

import pytest
from core.embeddings import Embedder
from core.embeddings.query_embeddings import embed_queries, sharing_query_embeddings


class GatedEmbedder(Embedder):
    """Records each ``embed_query`` call and holds it until ``release`` is set."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.release = asyncio.Event()
        self.release.set()
        self.cancelled = 0
        self._error = error

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("queries are embedded with embed_query")

    async def embed_query(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self._error is not None:
            raise self._error
        return [[float(len(text))] for text in texts]

    async def embed_single(self, text: str) -> list[float]:
        raise AssertionError("queries are embedded with embed_query")

    @property
    def dimension(self) -> int:
        return 1


@sharing_query_embeddings
async def _shared(work):
    return await work()


@pytest.mark.asyncio
async def test_outside_a_shared_retrieval_each_call_embeds():
    embedder = GatedEmbedder()

    await embed_queries(embedder, "field", ["q"])
    await embed_queries(embedder, "field", ["q"])

    assert embedder.calls == [["q"], ["q"]]


@pytest.mark.asyncio
async def test_concurrent_legs_on_one_space_share_one_embed():
    embedder = GatedEmbedder()
    embedder.release.clear()

    async def legs():
        first = asyncio.ensure_future(embed_queries(embedder, "field", ["q"]))
        second = asyncio.ensure_future(embed_queries(embedder, "field", ["q"]))
        await asyncio.sleep(0)
        embedder.release.set()
        return await asyncio.gather(first, second)

    assert await _shared(legs) == [[[1.0]], [[1.0]]]
    assert embedder.calls == [["q"]]


@pytest.mark.asyncio
async def test_a_later_leg_reuses_the_embedding():
    embedder = GatedEmbedder()

    async def legs():
        await embed_queries(embedder, "field", ["q"])
        return await embed_queries(embedder, "field", ["q"])

    assert await _shared(legs) == [[1.0]]
    assert embedder.calls == [["q"]]


@pytest.mark.asyncio
async def test_each_vector_space_embeds_on_its_own():
    first, second = GatedEmbedder(), GatedEmbedder()

    async def legs():
        await embed_queries(first, "field_a", ["q"])
        await embed_queries(second, "field_b", ["q"])

    await _shared(legs)

    assert first.calls == [["q"]]
    assert second.calls == [["q"]]


@pytest.mark.asyncio
async def test_only_texts_not_embedded_yet_are_sent_once_each_in_order():
    embedder = GatedEmbedder()

    async def legs():
        await embed_queries(embedder, "field", ["aa"])
        return await embed_queries(embedder, "field", ["bbb", "aa", "c", "bbb"])

    assert await _shared(legs) == [[3.0], [2.0], [1.0], [3.0]]
    assert embedder.calls == [["aa"], ["bbb", "c"]]


@pytest.mark.asyncio
async def test_a_failed_embed_fails_every_leg_waiting_on_it_once():
    embedder = GatedEmbedder(error=TimeoutError("embedder down"))
    embedder.release.clear()

    async def legs():
        first = asyncio.ensure_future(embed_queries(embedder, "field", ["q"]))
        second = asyncio.ensure_future(embed_queries(embedder, "field", ["q"]))
        await asyncio.sleep(0)
        embedder.release.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    results = await _shared(legs)

    assert [type(result) for result in results] == [TimeoutError, TimeoutError]
    assert embedder.calls == [["q"]]


@pytest.mark.asyncio
async def test_cancelling_one_leg_leaves_the_embed_to_the_others():
    embedder = GatedEmbedder()
    embedder.release.clear()

    async def legs():
        first = asyncio.ensure_future(embed_queries(embedder, "field", ["q"]))
        second = asyncio.ensure_future(embed_queries(embedder, "field", ["q"]))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        embedder.release.set()
        return await second

    assert await _shared(legs) == [[1.0]]
    assert embedder.calls == [["q"]]
    assert embedder.cancelled == 0


@pytest.mark.asyncio
async def test_an_embed_no_leg_waits_on_is_cancelled_when_the_retrieval_ends():
    embedder = GatedEmbedder()
    embedder.release.clear()

    async def legs():
        leg = asyncio.ensure_future(embed_queries(embedder, "field", ["q"]))
        await asyncio.sleep(0)
        leg.cancel()
        await asyncio.gather(leg, return_exceptions=True)

    await _shared(legs)
    await asyncio.sleep(0)

    assert embedder.cancelled == 1


@pytest.mark.asyncio
async def test_a_nested_retrieval_shares_the_outer_ones_embeddings():
    embedder = GatedEmbedder()

    async def inner():
        return await embed_queries(embedder, "field", ["q"])

    async def outer():
        await embed_queries(embedder, "field", ["q"])
        return await _shared(inner)

    assert await _shared(outer) == [[1.0]]
    assert embedder.calls == [["q"]]


@pytest.mark.asyncio
async def test_embeddings_are_not_kept_past_the_retrieval():
    embedder = GatedEmbedder()

    async def leg():
        return await embed_queries(embedder, "field", ["q"])

    await _shared(leg)
    await _shared(leg)

    assert embedder.calls == [["q"], ["q"]]
