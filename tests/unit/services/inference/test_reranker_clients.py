from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from core.utils.exceptions import InferenceConnectionError, InferenceTimeoutError
from services.inference.reranker_clients import InfinityReranker, OpenAIReranker, TEIReranker


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
    async def test_http_error_surfaces_response_body(self, reranker):
        # A 422 body names the exact rejected field; the raised error must carry
        # it (status code alone is undiagnosable in production).
        body = '{"detail":[{"loc":["body","top_n"],"msg":"field required"}]}'
        transport = httpx.MockTransport(lambda req: httpx.Response(422, text=body))
        reranker._client = httpx.AsyncClient(transport=transport)
        with pytest.raises(InferenceConnectionError) as exc:
            await reranker.rerank("query", DOCS)
        assert "422" in str(exc.value)
        assert "field required" in str(exc.value)

    @pytest.mark.asyncio
    async def test_trailing_slash_stripped(self):
        r = InfinityReranker(endpoint="http://reranker:7997/", model_name="m")
        assert r._endpoint == "http://reranker:7997"
        await r.aclose()


# TEI has its own wire format (``texts`` request field, bare-array response of
# ``{"index", "score"}``), so it needs its own coverage — not just the shared
# error-path tests.
def _tei_response(items: list[dict] | None = None) -> httpx.Response:
    items = (
        items
        if items is not None
        else [
            {"index": 1, "score": 0.8},
            {"index": 0, "score": 0.5},
            {"index": 2, "score": 0.2},
        ]
    )
    return httpx.Response(200, json=items)


class TestTEIReranker:
    @pytest.fixture
    def reranker(self):
        return TEIReranker(endpoint="http://reranker:8080", model_name="bge-reranker")

    @pytest.mark.asyncio
    async def test_rerank_parses_bare_array_and_sorts(self, reranker):
        # Response given out of order; client must sort by score desc.
        unsorted = [{"index": 0, "score": 0.5}, {"index": 1, "score": 0.8}, {"index": 2, "score": 0.2}]
        transport = httpx.MockTransport(lambda req: _tei_response(unsorted))
        reranker._client = httpx.AsyncClient(transport=transport)
        result = await reranker.rerank("query", DOCS)
        assert result == [(1, 0.8), (0, 0.5), (2, 0.2)]

    @pytest.mark.asyncio
    async def test_sends_texts_field_not_documents(self, reranker):
        captured = {}

        def capture(req):
            import json

            captured.update(json.loads(req.content))
            return _tei_response()

        transport = httpx.MockTransport(capture)
        reranker._client = httpx.AsyncClient(transport=transport)
        await reranker.rerank("query", DOCS)
        # TEI's contract: `texts`, no `documents`/`model`/`top_n`.
        assert captured["texts"] == DOCS
        assert "documents" not in captured
        assert "top_n" not in captured
        assert "model" not in captured

    @pytest.mark.asyncio
    async def test_top_k_truncates_after_sort(self, reranker):
        transport = httpx.MockTransport(lambda req: _tei_response())
        reranker._client = httpx.AsyncClient(transport=transport)
        result = await reranker.rerank("query", DOCS, top_k=2)
        assert result == [(1, 0.8), (0, 0.5)]

    @pytest.mark.asyncio
    async def test_missing_score_field_is_mapped_error(self, reranker):
        # A drifted wire format (no `score`) must become a mapped inference error,
        # not an uncaught KeyError.
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json=[{"index": 0}]))
        reranker._client = httpx.AsyncClient(transport=transport)
        with pytest.raises(InferenceConnectionError):
            await reranker.rerank("query", DOCS)

    @pytest.mark.asyncio
    async def test_http_error_surfaces_response_body(self, reranker):
        body = '{"error":"Input validation error: `texts` must be non-empty"}'
        transport = httpx.MockTransport(lambda req: httpx.Response(422, text=body))
        reranker._client = httpx.AsyncClient(transport=transport)
        with pytest.raises(InferenceConnectionError) as exc:
            await reranker.rerank("query", DOCS)
        assert "422" in str(exc.value)
        assert "must be non-empty" in str(exc.value)

    @pytest.mark.asyncio
    async def test_connection_error(self, reranker):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        reranker._client = AsyncMock()
        reranker._client.post = fail
        with pytest.raises(InferenceConnectionError):
            await reranker.rerank("query", DOCS)


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

    @pytest.mark.asyncio
    async def test_http_error_surfaces_response_body(self, reranker):
        body = '{"detail":[{"loc":["body","documents"],"msg":"field required"}]}'
        transport = httpx.MockTransport(lambda req: httpx.Response(422, text=body))
        reranker._client = httpx.AsyncClient(transport=transport)
        with pytest.raises(InferenceConnectionError) as exc:
            await reranker.rerank("query", DOCS)
        assert "422" in str(exc.value)
        assert "field required" in str(exc.value)


class TestRegistryIntegration:
    def test_infinity_registered(self):
        from core.rerankers import reranker_registry

        assert "infinity" in reranker_registry

    def test_openai_registered(self):
        from core.rerankers import reranker_registry

        assert "openai" in reranker_registry

    def test_tei_registered(self):
        from core.rerankers import reranker_registry

        assert "tei" in reranker_registry
