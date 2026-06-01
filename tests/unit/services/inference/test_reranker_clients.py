from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from core.utils.exceptions import InferenceConnectionError, InferenceTimeoutError
from services.inference.reranker_clients import InfinityReranker, OpenAIReranker


def _rerank_response(results: list[dict] | None = None) -> httpx.Response:
    results = results or [
        {"index": 0, "relevance_score": 0.9},
        {"index": 2, "relevance_score": 0.7},
        {"index": 1, "relevance_score": 0.3},
    ]
    return httpx.Response(200, json={"results": results})


DOCS = ["doc zero", "doc one", "doc two"]


class TestInfinityReranker:
    @pytest.fixture
    def reranker(self):
        return InfinityReranker(endpoint="http://reranker:7997", model_name="gte-reranker")

    @pytest.mark.asyncio
    async def test_rerank(self, reranker):
        transport = httpx.MockTransport(lambda req: _rerank_response())
        reranker._client = httpx.AsyncClient(transport=transport)
        result = await reranker.rerank("query", DOCS)
        assert result == [(0, 0.9), (2, 0.7), (1, 0.3)]

    @pytest.mark.asyncio
    async def test_rerank_with_top_k(self, reranker):
        captured = {}

        def capture(req):
            import json

            captured.update(json.loads(req.content))
            return _rerank_response([{"index": 0, "relevance_score": 0.9}])

        transport = httpx.MockTransport(capture)
        reranker._client = httpx.AsyncClient(transport=transport)
        result = await reranker.rerank("query", DOCS, top_k=1)
        assert captured["top_n"] == 1
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_top_k_clamped_to_doc_count(self, reranker):
        captured = {}

        def capture(req):
            import json

            captured.update(json.loads(req.content))
            return _rerank_response()

        transport = httpx.MockTransport(capture)
        reranker._client = httpx.AsyncClient(transport=transport)
        await reranker.rerank("query", DOCS, top_k=100)
        assert captured["top_n"] == 3

    @pytest.mark.asyncio
    async def test_sends_raw_scores(self, reranker):
        captured = {}

        def capture(req):
            import json

            captured.update(json.loads(req.content))
            return _rerank_response()

        transport = httpx.MockTransport(capture)
        reranker._client = httpx.AsyncClient(transport=transport)
        await reranker.rerank("query", DOCS)
        assert captured["raw_scores"] is True
        assert captured["return_documents"] is False

    @pytest.mark.asyncio
    async def test_connection_error(self, reranker):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        reranker._client = AsyncMock()
        reranker._client.post = fail
        with pytest.raises(InferenceConnectionError):
            await reranker.rerank("query", DOCS)

    @pytest.mark.asyncio
    async def test_timeout(self, reranker):
        async def fail(*a, **kw):
            raise httpx.TimeoutException("timeout")

        reranker._client = AsyncMock()
        reranker._client.post = fail
        with pytest.raises(InferenceTimeoutError):
            await reranker.rerank("query", DOCS)

    @pytest.mark.asyncio
    async def test_trailing_slash_stripped(self):
        r = InfinityReranker(endpoint="http://reranker:7997/", model_name="m")
        assert r._endpoint == "http://reranker:7997"
        await r.aclose()


class TestOpenAIReranker:
    @pytest.fixture
    def reranker(self):
        return OpenAIReranker(endpoint="http://reranker:8000/v1", model_name="gte-reranker", api_key="k")

    @pytest.mark.asyncio
    async def test_rerank(self, reranker):
        transport = httpx.MockTransport(lambda req: _rerank_response())
        reranker._client = httpx.AsyncClient(transport=transport)
        result = await reranker.rerank("query", DOCS)
        assert result == [(0, 0.9), (2, 0.7), (1, 0.3)]

    @pytest.mark.asyncio
    async def test_connection_error(self, reranker):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        reranker._client = AsyncMock()
        reranker._client.post = fail
        with pytest.raises(InferenceConnectionError):
            await reranker.rerank("query", DOCS)

    @pytest.mark.asyncio
    async def test_timeout(self, reranker):
        async def fail(*a, **kw):
            raise httpx.TimeoutException("timeout")

        reranker._client = AsyncMock()
        reranker._client.post = fail
        with pytest.raises(InferenceTimeoutError):
            await reranker.rerank("query", DOCS)


class TestRegistryIntegration:
    def test_infinity_registered(self):
        from core.rerankers import reranker_registry

        assert "infinity" in reranker_registry

    def test_openai_registered(self):
        from core.rerankers import reranker_registry

        assert "openai" in reranker_registry
