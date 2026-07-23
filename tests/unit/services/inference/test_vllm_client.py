from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import tenacity
from core.utils.exceptions import (
    EmbeddingAPIError,
    EmbeddingResponseError,
    InferenceConnectionError,
    InferenceError,
    InferenceTimeoutError,
)
from services.inference._circuit_breaker import _breakers
from services.inference.vllm_client import (
    _SUSPECT_UNICODE_ESCAPE,
    VLLMClient,
    VLLMEmbedder,
    VLLMVision,
    _find_suspect_escapes,
    _log_safe_error_detail,
)


@pytest.fixture(autouse=True)
def _clean_breakers():
    yield
    for breaker in _breakers.values():
        breaker.close()
    _breakers.clear()


def _make_transport(handler):
    return httpx.MockTransport(handler)


def _chat_response(content: str = "hello") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _completions_response(text: str = "result") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"text": text}]})


def _embed_response(vectors: list[list[float]] | None = None) -> httpx.Response:
    vectors = vectors or [[0.1, 0.2, 0.3]]
    data = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
    return httpx.Response(200, json={"data": data})


# ---------------------------------------------------------------------------
# VLLMClient (LLM)
# ---------------------------------------------------------------------------


class TestVLLMClient:
    def _make_client(self, handler, **kwargs):
        client = VLLMClient(
            endpoint="http://vllm:8000/v1",
            model_name="test-model",
            api_key="test-key",
            temperature=0.3,
            **kwargs,
        )
        client._client = httpx.AsyncClient(transport=_make_transport(handler))
        return client

    @pytest.mark.asyncio
    async def test_chat_returns_full_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "/chat/completions" in str(request.url)
            assert body["model"] == "test-model"
            assert body["stream"] is False
            assert body["temperature"] == 0.3
            return _chat_response("world")

        result = await self._make_client(handler).chat([{"role": "user", "content": "hi"}])
        assert result["choices"][0]["message"]["content"] == "world"

    @pytest.mark.asyncio
    async def test_generate_returns_full_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "/completions" in str(request.url)
            assert "/chat/" not in str(request.url)
            assert body["prompt"] == "say something"
            return _completions_response("done")

        result = await self._make_client(handler).generate("say something")
        assert result["choices"][0]["text"] == "done"

    @pytest.mark.asyncio
    async def test_stream_chat_yields_raw_sse_lines(self):
        sse_body = (
            'data: {"choices":[{"delta":{"content":"Hello"}}]}\n'
            'data: {"choices":[{"delta":{"content":" world"}}]}\n'
            "data: [DONE]\n"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(200, text=sse_body)

        client = self._make_client(handler)
        lines = [line async for line in client.stream_chat([{"role": "user", "content": "hi"}])]
        assert 'data: {"choices":[{"delta":{"content":"Hello"}}]}' in lines
        assert 'data: {"choices":[{"delta":{"content":" world"}}]}' in lines

    @pytest.mark.asyncio
    async def test_stream_chat_sends_enable_thinking_as_chat_template_kwargs_when_configured(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["stream"] is True
            assert body["chat_template_kwargs"] == {"enable_thinking": True}
            assert "enable_thinking" not in body
            return httpx.Response(200, text="data: [DONE]\n")

        client = self._make_client(handler, enable_thinking=True)
        lines = [line async for line in client.stream_chat([{"role": "user", "content": "hi"}])]

        assert lines == ["data: [DONE]"]

    @pytest.mark.asyncio
    async def test_stream_chat_error_raises(self):
        client = self._make_client(lambda req: httpx.Response(503, text="unavailable"))
        with pytest.raises(InferenceError):
            async for _ in client.stream_chat([{"role": "user", "content": "hi"}]):
                pass

    @pytest.mark.asyncio
    async def test_chat_connection_error(self):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        client = VLLMClient(endpoint="http://vllm:8000/v1", model_name="m")
        client._client = AsyncMock()
        client._client.post = fail
        with pytest.raises(InferenceConnectionError):
            await client.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_chat_timeout(self):
        async def fail(*a, **kw):
            raise httpx.TimeoutException("timeout")

        client = VLLMClient(endpoint="http://vllm:8000/v1", model_name="m")
        client._client = AsyncMock()
        client._client.post = fail
        with pytest.raises(InferenceTimeoutError):
            await client.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_defaults_forwarded(self):
        captured: dict = {}

        def capture(req: httpx.Request) -> httpx.Response:
            captured.update(json.loads(req.content))
            return _chat_response()

        await self._make_client(capture).chat([{"role": "user", "content": "hi"}])
        assert captured["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_enable_thinking_is_sent_as_chat_template_kwargs_when_configured(self):
        captured: dict = {}

        def capture(req: httpx.Request) -> httpx.Response:
            captured.update(json.loads(req.content))
            return _chat_response()

        await self._make_client(capture, enable_thinking=True).chat([{"role": "user", "content": "hi"}])

        assert captured["chat_template_kwargs"] == {"enable_thinking": True}
        assert "enable_thinking" not in captured

    @pytest.mark.asyncio
    async def test_enable_thinking_merges_with_existing_chat_template_kwargs(self):
        captured: dict = {}

        def capture(req: httpx.Request) -> httpx.Response:
            captured.update(json.loads(req.content))
            return _chat_response()

        await self._make_client(
            capture,
            enable_thinking=True,
            chat_template_kwargs={"custom": "value"},
        ).chat([{"role": "user", "content": "hi"}])

        assert captured["chat_template_kwargs"] == {"custom": "value", "enable_thinking": True}

    @pytest.mark.asyncio
    async def test_chat_template_kwargs_omitted_by_default(self):
        captured: dict = {}

        def capture(req: httpx.Request) -> httpx.Response:
            captured.update(json.loads(req.content))
            return _chat_response()

        await self._make_client(capture).chat([{"role": "user", "content": "hi"}])

        assert "chat_template_kwargs" not in captured

    @pytest.mark.asyncio
    async def test_per_request_kwargs_override_defaults(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["temperature"] == 0.9
            assert body["max_tokens"] == 100
            return _chat_response()

        await self._make_client(handler).chat([{"role": "user", "content": "hi"}], temperature=0.9, max_tokens=100)

    @pytest.mark.asyncio
    async def test_max_retries_is_not_forwarded_to_request_body(self):
        """max_retries is a config field (LLMParamsConfig), not a sampling param —
        it must not leak into **kwargs/self._defaults and end up in the outgoing
        chat payload the way batch_size once did for the embedder (#712)."""
        captured: dict = {}

        def capture(req: httpx.Request) -> httpx.Response:
            captured.update(json.loads(req.content))
            return _chat_response()

        client = self._make_client(capture, max_retries=9)
        assert "max_retries" not in client._defaults

        await client.chat([{"role": "user", "content": "hi"}])

        assert "max_retries" not in captured

    @pytest.mark.asyncio
    async def test_trailing_slash_stripped(self):
        c = VLLMClient(endpoint="http://vllm:8000/v1/", model_name="m")
        assert c._endpoint == "http://vllm:8000/v1"
        await c.aclose()

    @pytest.mark.asyncio
    async def test_aclose(self):
        client = VLLMClient(endpoint="http://vllm:8000/v1", model_name="m")
        client._client = AsyncMock()
        await client.aclose()
        client._client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# VLLMClientOverrides
# ---------------------------------------------------------------------------


class TestVLLMClientOverrides:
    """Tests for _resolve_overrides (partition-level model selection)."""

    def _make_client(self):
        return VLLMClient(
            endpoint="http://default:8000/v1",
            model_name="default-model",
            api_key="default-key",
        )

    def test_no_override_uses_defaults(self):
        client = self._make_client()
        kwargs: dict = {}
        base_url, model, headers = client._resolve_overrides(kwargs)
        assert base_url == "http://default:8000/v1"
        assert model == "default-model"
        assert headers is None

    def test_llm_override_model_applied_endpoint_and_key_ignored(self):
        client = self._make_client()
        original_metadata = {
            "llm_override": {
                "base_url": "http://custom:9000/v1/",
                "api_key": "custom-key",
                "model": "custom-model",
            },
        }
        kwargs: dict = {"metadata": original_metadata}
        base_url, model, headers = client._resolve_overrides(kwargs)
        # Only `model` is honored; endpoint and credentials stay server-side.
        assert model == "custom-model"
        assert base_url == "http://default:8000/v1"
        assert headers is None
        # kwargs must not be mutated — retries depend on llm_override surviving.
        assert kwargs["metadata"] is original_metadata
        assert "llm_override" in kwargs["metadata"]

    def test_llm_override_partial(self):
        client = self._make_client()
        original_metadata = {
            "llm_override": {"model": "override-model"},
            "use_map_reduce": True,
        }
        kwargs: dict = {"metadata": original_metadata}
        base_url, model, headers = client._resolve_overrides(kwargs)
        assert base_url == "http://default:8000/v1"
        assert model == "override-model"
        assert headers is None
        assert kwargs["metadata"] is original_metadata
        assert kwargs["metadata"] == {
            "llm_override": {"model": "override-model"},
            "use_map_reduce": True,
        }

    def test_client_base_url_and_api_key_override_ignored(self):
        # SSRF / key-exfiltration guard: a client-supplied base_url/api_key
        # must never be honored.
        client = self._make_client()
        kwargs: dict = {
            "metadata": {
                "llm_override": {
                    "base_url": "http://169.254.169.254/latest/meta-data",
                    "api_key": "attacker-key",
                    "model": "custom-model",
                }
            }
        }
        base_url, model, headers = client._resolve_overrides(kwargs)
        assert model == "custom-model"
        assert base_url == "http://default:8000/v1"
        assert headers is None


# ---------------------------------------------------------------------------
# VLLMEmbedder
# ---------------------------------------------------------------------------


class TestVLLMEmbedder:
    def _make_embedder(self, handler, **kwargs):
        embedder = VLLMEmbedder(
            endpoint="http://vllm:8000/v1",
            model_name="bge-m3",
            api_key="test-key",
            **kwargs,
        )
        embedder._client = httpx.AsyncClient(transport=_make_transport(handler))
        return embedder

    @pytest.mark.asyncio
    async def test_embed_returns_sorted_vectors(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["model"] == "bge-m3"
            assert body["input"] == ["hello", "world"]
            return httpx.Response(
                200,
                json={"data": [{"index": 1, "embedding": [0.3, 0.4]}, {"index": 0, "embedding": [0.1, 0.2]}]},
            )

        result = await self._make_embedder(handler).embed(["hello", "world"])
        assert result == [[0.1, 0.2], [0.3, 0.4]]

    @pytest.mark.asyncio
    async def test_embed_splits_large_input_into_batches(self):
        """Inputs larger than batch_size are split into multiple requests and
        the vectors are reassembled in the original input order."""
        request_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            inputs = json.loads(request.content)["input"]
            request_sizes.append(len(inputs))
            # Echo each text's int value back as a 1-d vector so we can assert order.
            data = [{"index": i, "embedding": [float(t)]} for i, t in enumerate(inputs)]
            return httpx.Response(200, json={"data": data})

        texts = [str(i) for i in range(10)]
        embedder = self._make_embedder(handler, batch_size=4, embed_concurrency=2)
        result = await embedder.embed(texts)

        assert result == [[float(i)] for i in range(10)]  # order preserved across batches
        # 10 texts split into batches of 4; completion order is non-deterministic
        # under concurrency, so compare the multiset of request sizes.
        assert sorted(request_sizes) == [2, 4, 4]

    @pytest.mark.asyncio
    async def test_embed_cancels_pending_batches_on_failure(self):
        """When one batch fails, the other in-flight batches are cancelled
        instead of being left running and consuming embedder capacity."""
        embedder = VLLMEmbedder(endpoint="http://x", model_name="m", batch_size=1, embed_concurrency=5)
        started: list[str] = []
        cancelled: list[str] = []

        async def fake_embed_batch(texts: list[str], *, offset: int = 0) -> list[list[float]]:
            value = texts[0]
            started.append(value)
            if value == "fail":
                raise EmbeddingAPIError("boom", model_name="m", base_url="http://x", error="boom")
            try:
                await asyncio.sleep(10)  # slow batch, still in flight when 'fail' raises
                return [[0.0]]
            except asyncio.CancelledError:
                cancelled.append(value)
                raise

        embedder._embed_batch = fake_embed_batch  # type: ignore[method-assign]

        with pytest.raises(EmbeddingAPIError):
            await embedder.embed(["slow1", "fail", "slow2"])

        assert "fail" in started
        assert sorted(cancelled) == ["slow1", "slow2"]  # both slow batches were cancelled

    @pytest.mark.asyncio
    async def test_embed_empty_returns_empty(self):
        def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
            raise AssertionError("no request should be sent for empty input")

        assert await self._make_embedder(handler).embed([]) == []

    @pytest.mark.asyncio
    async def test_embed_single(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5, 0.6, 0.7]}]})

        result = await self._make_embedder(handler).embed_single("test")
        assert result == [0.5, 0.6, 0.7]

    @pytest.mark.asyncio
    async def test_dimension_auto_detected(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]})

        embedder = self._make_embedder(handler)

        with pytest.raises(RuntimeError, match="unknown"):
            _ = embedder.dimension

        await embedder.embed(["test"])
        assert embedder.dimension == 3

    def test_dimension_from_init(self):
        assert VLLMEmbedder(endpoint="http://x", model_name="m", dimension=768).dimension == 768

    @pytest.mark.asyncio
    async def test_truncate_prompt_tokens_is_one_below_max_model_len(self):
        # One token below max_model_len: vLLM pooling models hang on input that
        # is exactly max_model_len tokens long (vllm-project/vllm#29496).
        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["truncate_prompt_tokens"] == 8191
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

        await self._make_embedder(handler, max_model_len=8192).embed(["test"])

    @pytest.mark.asyncio
    async def test_truncate_prompt_tokens_floors_at_one(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["truncate_prompt_tokens"] == 1
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

        await self._make_embedder(handler, max_model_len=1).embed(["test"])

    @pytest.mark.asyncio
    async def test_truncate_prompt_tokens_absent_when_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "truncate_prompt_tokens" not in json.loads(request.content)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

        await self._make_embedder(handler).embed(["test"])

    @pytest.mark.asyncio
    async def test_embed_connection_error(self):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        embedder = VLLMEmbedder(endpoint="http://vllm:8000/v1", model_name="bge-m3")
        embedder._client = AsyncMock()
        embedder._client.post = fail
        with pytest.raises(EmbeddingAPIError):
            await embedder.embed(["text"])

    @pytest.mark.asyncio
    async def test_embed_timeout(self):
        async def fail(*a, **kw):
            raise httpx.TimeoutException("timeout")

        embedder = VLLMEmbedder(endpoint="http://vllm:8000/v1", model_name="bge-m3")
        embedder._client = AsyncMock()
        embedder._client.post = fail
        with pytest.raises(EmbeddingAPIError):
            await embedder.embed(["text"])

    @pytest.mark.asyncio
    async def test_embed_bad_response_format(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"wrong": "shape"})

        with pytest.raises(EmbeddingResponseError):
            await self._make_embedder(handler).embed(["text"])


# ---------------------------------------------------------------------------
# VLLMVision
# ---------------------------------------------------------------------------


class TestVLLMVision:
    def _make_vision(self, handler, **kwargs):
        vision = VLLMVision(
            endpoint="http://vllm:8000/v1",
            model_name="qwen-vl",
            api_key="test-key",
            **kwargs,
        )
        vision._client = httpx.AsyncClient(transport=_make_transport(handler))
        return vision

    @pytest.mark.asyncio
    async def test_caption_image(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["model"] == "qwen-vl"
            assert body["max_tokens"] == 1024
            msg = body["messages"][0]
            assert msg["content"][0]["type"] == "image_url"
            assert msg["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
            assert msg["content"][1]["type"] == "text"
            return _chat_response("A red car")

        result = await self._make_vision(handler).caption_image(b"\x89PNG\r\n\x1a\n", prompt="What is this?")
        assert result == "A red car"

    @pytest.mark.asyncio
    async def test_caption_image_default_prompt(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["messages"][0]["content"][1]["text"] == "Describe this image in detail."
            return _chat_response("An image")

        await self._make_vision(handler).caption_image(b"\x89PNG\r\n\x1a\n")

    @pytest.mark.asyncio
    async def test_caption_images_batch(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return _chat_response(f"Caption {call_count}")

        results = await self._make_vision(handler).caption_images_batch([b"img1", b"img2", b"img3"])
        assert len(results) == 3
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_custom_max_tokens(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["max_tokens"] == 512
            return _chat_response("ok")

        await self._make_vision(handler, max_tokens=512).caption_image(b"img")

    @pytest.mark.asyncio
    async def test_max_retries_is_not_forwarded_to_request_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "max_retries" not in json.loads(request.content)
            return _chat_response("ok")

        vision = self._make_vision(handler, max_retries=9)
        assert "max_retries" not in vision._defaults

        await vision.caption_image(b"img")

    @pytest.mark.asyncio
    async def test_caption_image_sends_enable_thinking_as_chat_template_kwargs_when_configured(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["chat_template_kwargs"] == {"enable_thinking": True}
            assert "enable_thinking" not in body
            return _chat_response("ok")

        await self._make_vision(handler, enable_thinking=True).caption_image(b"img")

    @pytest.mark.asyncio
    async def test_caption_image_merges_enable_thinking_with_existing_chat_template_kwargs(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["chat_template_kwargs"] == {"custom": "value", "enable_thinking": True}
            return _chat_response("ok")

        await self._make_vision(
            handler,
            enable_thinking=True,
            chat_template_kwargs={"custom": "value"},
        ).caption_image(b"img")

    @pytest.mark.asyncio
    async def test_caption_image_omits_chat_template_kwargs_by_default(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "chat_template_kwargs" not in json.loads(request.content)
            return _chat_response("ok")

        await self._make_vision(handler).caption_image(b"img")

    @pytest.mark.asyncio
    async def test_caption_connection_error(self):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        vision = VLLMVision(endpoint="http://vllm:8000/v1", model_name="qwen-vl")
        vision._client = AsyncMock()
        vision._client.post = fail
        with pytest.raises(InferenceConnectionError):
            await vision.caption_image(b"img")

    @pytest.mark.asyncio
    async def test_caption_timeout(self):
        async def fail(*a, **kw):
            raise httpx.TimeoutException("timeout")

        vision = VLLMVision(endpoint="http://vllm:8000/v1", model_name="qwen-vl")
        vision._client = AsyncMock()
        vision._client.post = fail
        with pytest.raises(InferenceTimeoutError):
            await vision.caption_image(b"img")


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    def test_llm_registered(self):
        from core.llm import llm_registry

        assert "vllm" in llm_registry

    def test_embedder_registered(self):
        from core.embeddings import embedder_registry

        assert "vllm" in embedder_registry

    def test_vlm_registered(self):
        from core.vlm import vlm_registry

        assert "vllm" in vlm_registry


# ---------------------------------------------------------------------------
# #704 — embedder transport failures must actually be retried
# ---------------------------------------------------------------------------


class TestEmbedderRetryFires:
    """Assert the retry *re-invokes*, not merely that an exception type changed.

    Before #704, ``EmbeddingAPIError`` hardcoded ``status_code=500``. Since
    ``_is_retryable`` only accepts {429, 502, 503, 504} and the httpx exception
    is translated *inside* the retried body, ``@with_retry(max_attempts=3)`` on
    ``_embed_batch`` could never fire — one transient 503 failed the whole file.
    """

    @pytest.fixture(autouse=True)
    def _no_backoff(self):
        """Drop the exponential backoff so these tests don't really sleep.

        The retry decorator is applied at import time, so patching the module
        function has no effect — reach into the tenacity Retrying object the
        decorator attached and restore it afterwards.
        """
        retrying = VLLMEmbedder._embed_batch.retry
        original = retrying.wait
        retrying.wait = tenacity.wait_none()
        yield
        retrying.wait = original

    def _embedder(self, handler):
        embedder = VLLMEmbedder(
            endpoint="http://embed.test/v1",
            model_name="embed-model",
            batch_size=8,
        )
        embedder._client = httpx.AsyncClient(transport=_make_transport(handler))
        return embedder

    @pytest.mark.asyncio
    async def test_retries_then_succeeds_on_transient_503(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503, text="upstream busy")
            return _embed_response([[0.1, 0.2]])

        embedder = self._embedder(handler)
        out = await embedder._embed_batch(["hello"])

        assert out == [[0.1, 0.2]]
        assert calls["n"] == 3, "expected two retries before success"

    @pytest.mark.asyncio
    async def test_retries_on_429_rate_limit(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(429, text="slow down")
            return _embed_response([[1.0]])

        embedder = self._embedder(handler)
        assert await embedder._embed_batch(["x"]) == [[1.0]]
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_retries_on_connection_reset_mid_request(self):
        """A connection reset after the request starts is httpx.ReadError, not
        ConnectError (a NetworkError under TransportError). The first fix only
        caught ConnectError/TimeoutException, so a reset escaped raw and was not
        retried. Regression for the #718 review."""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ReadError("connection reset by peer")
            return _embed_response([[0.5]])

        embedder = self._embedder(handler)
        assert await embedder._embed_batch(["x"]) == [[0.5]]
        assert calls["n"] == 2, "a ReadError mid-request must be retried"

    @pytest.mark.asyncio
    async def test_retries_on_connect_error(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectError("cannot connect")
            return _embed_response([[0.6]])

        embedder = self._embedder(handler)
        assert await embedder._embed_batch(["x"]) == [[0.6]]
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_client_error(self):
        """A 400 is not transient — it must fail fast, not burn three attempts."""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(400, text="bad request")

        embedder = self._embedder(handler)
        with pytest.raises(EmbeddingAPIError):
            await embedder._embed_batch(["x"])
        assert calls["n"] == 1, "4xx must not be retried"


class TestEmbedderErrorPayloadIsBounded:
    """``EmbeddingAPIError.extra`` is not log-only — error_handlers.py serializes
    it into the HTTP error body, so anything put there is client-visible and has
    to stay bounded.
    """

    def _embedder(self, handler):
        embedder = VLLMEmbedder(endpoint="http://embed.test/v1", model_name="embed-model", batch_size=8)
        embedder._client = httpx.AsyncClient(transport=_make_transport(handler))
        return embedder

    @pytest.mark.asyncio
    async def test_upstream_error_body_is_truncated(self):
        """A huge upstream error page (e.g. an HTML 400) must not be echoed whole."""
        embedder = self._embedder(lambda request: httpx.Response(400, text="E" * 10_000))

        with pytest.raises(EmbeddingAPIError) as excinfo:
            await embedder._embed_batch(["x"])

        assert len(excinfo.value.extra["error"]) == 500

    def test_suspect_findings_are_capped(self):
        r"""One snippet per offending chunk would mirror a whole batch of document
        text into the payload; a few examples identify the problem just as well."""
        findings = _find_suspect_escapes([f"bad \\umlaut {i}" for i in range(50)])

        assert len(findings) == 5

    def test_suspect_snippet_is_a_window_not_the_whole_chunk(self):
        """The snippet is a window around the match, so a multi-KB chunk
        contributes only its neighbourhood of the offending escape."""
        findings = _find_suspect_escapes(["A" * 5_000 + "\\umlaut" + "B" * 5_000])

        assert len(findings) == 1
        # 15 chars either side of the 2-char `\u` match.
        assert len(findings[0]["snippet"]) == 32


class TestEmbedderFailureLogOmitsDocumentText:
    """The log pipeline has a different audience from the API error: operators
    read container / centralized logs across tenants, while the API error goes
    back to the uploader who already owns the document. Document text may reach
    the second but never the first.
    """

    def test_log_detail_carries_positions_not_snippets(self):
        exc = EmbeddingAPIError(
            "Embedder API error (400)",
            status_code=400,
            model_name="embed-model",
            base_url="http://embed.test/v1",
            error='{"message": "invalid character in \\u escape"}',
            suspect_texts=[
                {"index": 3, "snippet": "contrat de M. Dupont \\uXY salaire"},
                {"index": 7, "snippet": "numero de securite sociale \\uZZ"},
            ],
        )

        detail = _log_safe_error_detail(exc)

        assert detail["suspect_count"] == 2
        assert detail["suspect_indices"] == [3, 7]
        assert detail["status_code"] == 400
        assert detail["model_name"] == "embed-model"
        # The point of the whole helper: no document text, and not the
        # provider body either (it can echo the rejected input back).
        flat = repr(detail)
        assert "Dupont" not in flat
        assert "securite sociale" not in flat
        assert "snippet" not in flat
        assert "invalid character" not in flat

    def test_log_detail_is_empty_for_an_exception_without_extra(self):
        """`except BaseException` also catches plain exceptions — no `.extra`,
        nothing to summarize, and no crash in the failure path."""
        assert _log_safe_error_detail(RuntimeError("boom")) == {}


class TestSuspectIndexIsDocumentGlobal:
    """The reported position must identify the chunk in the *document*.

    `_embed_batch` only ever sees one batch, so a raw `enumerate` position is
    batch-local: under `batch_size=32` the offending chunk 97 reports as 1, and
    every batch reports the same 0..31 range. Since the failure log carries only
    these positions, a batch-local one silently points at an innocent chunk.
    """

    def test_offset_shifts_reported_positions(self):
        findings = _find_suspect_escapes(["fine", "bad \\umlaut"], offset=96)

        assert [f["index"] for f in findings] == [97]

    def test_offset_defaults_to_zero_for_a_single_batch(self):
        findings = _find_suspect_escapes(["bad \\umlaut"])

        assert [f["index"] for f in findings] == [0]

    @pytest.mark.asyncio
    async def test_multi_batch_embed_reports_the_document_position(self):
        """End-to-end through `embed()`: the bad chunk sits in the fourth batch,
        so a batch-local index would report 1 instead of 97."""
        texts = ["clean chunk"] * 200
        texts[97] = "contract \\umlaut clause"

        def handler(request):
            payload = json.loads(request.content.decode())
            if any(_SUSPECT_UNICODE_ESCAPE.search(t) for t in payload["input"]):
                return httpx.Response(400, text="invalid character in \\u escape")
            return _embed_response([[0.1]] * len(payload["input"]))

        embedder = VLLMEmbedder(endpoint="http://embed.test/v1", model_name="embed-model", batch_size=32)
        embedder._client = httpx.AsyncClient(transport=_make_transport(handler))

        with pytest.raises(EmbeddingAPIError) as excinfo:
            await embedder.embed(texts)

        assert [f["index"] for f in excinfo.value.extra["suspect_texts"]] == [97]
