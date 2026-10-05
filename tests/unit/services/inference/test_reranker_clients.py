from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from core.utils.exceptions import CircuitBreakerOpenError, InferenceConnectionError, InferenceTimeoutError
from services.inference import _metrics
from services.inference._circuit_breaker import _breakers, _is_excluded
from services.inference._metrics import outcome_for
from services.inference._retry import _is_retryable
from services.inference.reranker_clients import (
    InfinityReranker,
    OpenAIReranker,
    TEIReranker,
    _raise_reranker_http_error,
)


@pytest.fixture(autouse=True)
def _clean_breakers():
    yield
    for breaker in _breakers.values():
        breaker.close()
    _breakers.clear()


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
    async def test_http_error_keeps_response_body_out_of_message(self, reranker):
        # The 422 body names the rejected field, but the exception message
        # reaches API clients verbatim (SSE errors / 503 detail) — the body is
        # logged for operators, never embedded in the message.
        body = '{"detail":[{"loc":["body","top_n"],"msg":"field required"}]}'
        transport = httpx.MockTransport(lambda req: httpx.Response(422, text=body))
        reranker._client = httpx.AsyncClient(transport=transport)
        with pytest.raises(InferenceConnectionError) as exc:
            await reranker.rerank("query", DOCS)
        assert "422" in str(exc.value)
        assert "field required" not in str(exc.value)

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
        # TEI's contract: `texts`, no `documents`/`model`/`top_n`; `truncate`
        # so one over-long text doesn't fail the whole request.
        assert captured["texts"] == DOCS
        assert captured["truncate"] is True
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
    async def test_http_error_keeps_response_body_out_of_message(self, reranker):
        body = '{"error":"Input validation error: `texts` must be non-empty"}'
        transport = httpx.MockTransport(lambda req: httpx.Response(422, text=body))
        reranker._client = httpx.AsyncClient(transport=transport)
        with pytest.raises(InferenceConnectionError) as exc:
            await reranker.rerank("query", DOCS)
        assert "422" in str(exc.value)
        assert "must be non-empty" not in str(exc.value)

    @pytest.mark.asyncio
    async def test_connection_error(self, reranker):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        reranker._client = AsyncMock()
        reranker._client.post = fail
        with pytest.raises(InferenceConnectionError):
            await reranker.rerank("query", DOCS)

    @pytest.mark.asyncio
    async def test_batches_requests_over_tei_client_batch_limit(self, reranker):
        # TEI rejects requests with more texts than --max-client-batch-size
        # (default 32), so a 50-doc rerank must be split into two requests and
        # each batch's local indices shifted back to full-list positions.
        docs = [f"doc-{i}" for i in range(50)]
        batch_sizes = []

        def handler(req):
            import json

            texts = json.loads(req.content)["texts"]
            batch_sizes.append(len(texts))
            # Score each text by its global doc number so merged ordering is checkable.
            items = [{"index": i, "score": int(t.split("-")[1]) / 100} for i, t in enumerate(texts)]
            return httpx.Response(200, json=items)

        reranker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await reranker.rerank("query", docs)
        assert sorted(batch_sizes) == [18, 32]
        assert [idx for idx, _ in result] == list(range(49, -1, -1))

    @pytest.mark.asyncio
    async def test_top_k_truncates_across_batches(self, reranker):
        # The top-scored docs live in the second batch; top_k must apply to the
        # merged, globally sorted results — not per batch.
        docs = [f"doc-{i}" for i in range(40)]

        def handler(req):
            import json

            texts = json.loads(req.content)["texts"]
            items = [{"index": i, "score": int(t.split("-")[1]) / 100} for i, t in enumerate(texts)]
            return httpx.Response(200, json=items)

        reranker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await reranker.rerank("query", docs, top_k=3)
        assert [idx for idx, _ in result] == [39, 38, 37]


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
    async def test_http_error_keeps_response_body_out_of_message(self, reranker):
        body = '{"detail":[{"loc":["body","documents"],"msg":"field required"}]}'
        transport = httpx.MockTransport(lambda req: httpx.Response(422, text=body))
        reranker._client = httpx.AsyncClient(transport=transport)
        with pytest.raises(InferenceConnectionError) as exc:
            await reranker.rerank("query", DOCS)
        assert "422" in str(exc.value)
        assert "field required" not in str(exc.value)


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


def _reranker_http_error(status: int) -> InferenceConnectionError:
    """The exception every reranker client raises for a non-2xx *status*."""
    request = httpx.Request("POST", "http://reranker.invalid/rerank")
    response = httpx.Response(status, request=request)
    try:
        _raise_reranker_http_error(
            "http://reranker.invalid", httpx.HTTPStatusError("refused", request=request, response=response)
        )
    except InferenceConnectionError as exc:
        return exc
    raise AssertionError("_raise_reranker_http_error returned")


class TestRerankerHttpErrorStatus:
    """The breaker, the retry and the metrics all read ``status_code``. Without
    the upstream status every reranker reply read as 503: a 401 from one
    endpoint opened the ``reranker`` breaker every endpoint shares (#1100)."""

    @pytest.mark.parametrize(
        ("status", "counts_on_breaker", "outcome", "retried"),
        [
            (400, False, "rejected", False),
            (401, False, "error", False),
            (403, False, "rejected", False),
            (404, False, "rejected", False),
            (408, False, "error", False),
            (429, False, "error", True),
            (500, True, "error", False),
            (503, True, "error", True),
        ],
    )
    def test_the_upstream_status_reaches_the_breaker_retry_and_metrics(
        self, status: int, counts_on_breaker: bool, outcome: str, retried: bool
    ) -> None:
        exc = _reranker_http_error(status)

        assert exc.status_code == status
        assert _is_excluded(exc) is not counts_on_breaker
        assert outcome_for(exc, operation="rerank") == outcome
        assert _is_retryable(exc) is retried


class TestRerankerBreakerEndToEnd:
    @pytest.fixture
    def recorded(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        calls: list[dict] = []
        monkeypatch.setattr(_metrics, "record_inference", lambda **kw: calls.append(kw))
        return calls

    @pytest.fixture(autouse=True)
    def _no_retry_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda _seconds, *a, **kw: real_sleep(0))

    @staticmethod
    def _reranker(endpoint: str, handler) -> InfinityReranker:
        reranker = InfinityReranker(endpoint=endpoint, model_name="gte-reranker")
        reranker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return reranker

    @pytest.mark.asyncio
    async def test_one_endpoints_bad_key_does_not_stop_the_others(self, recorded: list[dict]) -> None:
        """More refused calls than the breaker's ``fail_max`` (50): the healthy
        endpoint behind the same breaker still answers, and each refusal is
        recorded as the refusing endpoint's ``error``."""
        revoked = self._reranker("http://revoked:7997", lambda req: httpx.Response(401, json={"error": "bad key"}))
        healthy = self._reranker("http://healthy:7997", lambda req: _rerank_response())

        for _ in range(60):
            with pytest.raises(InferenceConnectionError) as info:
                await revoked.rerank("query", DOCS)
            assert not isinstance(info.value, CircuitBreakerOpenError)

        assert _breakers["reranker"].fail_counter == 0
        assert await healthy.rerank("query", DOCS) == [(0, 0.9), (2, 0.7), (1, 0.3)]
        assert [c["outcome"] for c in recorded] == ["error"] * 60 + ["success"]

    @pytest.mark.asyncio
    async def test_provider_failures_still_open_the_breaker(self) -> None:
        """Control for the test above: a 503 is the provider failing, and trips
        the shared breaker for every reranker endpoint."""
        down = self._reranker("http://down:7997", lambda req: httpx.Response(503))
        healthy = self._reranker("http://healthy:7997", lambda req: _rerank_response())

        # The 50th failure trips it and is itself reported as open.
        for _ in range(49):
            with pytest.raises(InferenceConnectionError):
                await down.rerank("query", DOCS)
        with pytest.raises(CircuitBreakerOpenError):
            await down.rerank("query", DOCS)

        with pytest.raises(CircuitBreakerOpenError):
            await healthy.rerank("query", DOCS)
