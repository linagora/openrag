from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from core.utils.exceptions import (
    EmbeddingAPIError,
    EmbeddingResponseError,
    InferenceConnectionError,
    InferenceError,
    InferenceTimeoutError,
)
from services.inference._circuit_breaker import _breakers

from .ollama_client import OllamaClient, OllamaEmbedder


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
# OllamaClient (LLM)
# ---------------------------------------------------------------------------


class TestOllamaClient:
    def _make_client(self, handler, endpoint="http://ollama:11434", **kwargs):
        client = OllamaClient(endpoint=endpoint, model_name="llama3", **kwargs)
        client._client = httpx.AsyncClient(transport=_make_transport(handler))
        return client

    @pytest.mark.asyncio
    async def test_chat_returns_full_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "/v1/chat/completions" in str(request.url)
            assert body["model"] == "llama3"
            assert body["stream"] is False
            return _chat_response("world")

        result = await self._make_client(handler).chat([{"role": "user", "content": "hi"}])
        assert result["choices"][0]["message"]["content"] == "world"

    @pytest.mark.asyncio
    async def test_generate_returns_full_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "/v1/completions" in str(request.url)
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
    async def test_stream_chat_error_raises(self):
        client = self._make_client(lambda req: httpx.Response(503, text="unavailable"))
        with pytest.raises(InferenceError):
            async for _ in client.stream_chat([{"role": "user", "content": "hi"}]):
                pass

    @pytest.mark.asyncio
    async def test_chat_connection_error(self):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        client = OllamaClient(endpoint="http://ollama:11434", model_name="llama3")
        client._client = AsyncMock()
        client._client.post = fail
        with pytest.raises(InferenceConnectionError):
            await client.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_chat_timeout(self):
        async def fail(*a, **kw):
            raise httpx.TimeoutException("timeout")

        client = OllamaClient(endpoint="http://ollama:11434", model_name="llama3")
        client._client = AsyncMock()
        client._client.post = fail
        with pytest.raises(InferenceTimeoutError):
            await client.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_chat_http_error_raises_inference_error(self):
        client = self._make_client(lambda req: httpx.Response(500, text="server error"))
        with pytest.raises(InferenceError):
            await client.chat([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_defaults_forwarded(self):
        captured: dict = {}

        def capture(req: httpx.Request) -> httpx.Response:
            captured.update(json.loads(req.content))
            return _chat_response()

        await self._make_client(capture, temperature=0.7).chat([{"role": "user", "content": "hi"}])
        assert captured["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_per_request_kwargs_override_defaults(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["temperature"] == 0.1
            assert body["max_tokens"] == 256
            return _chat_response()

        await self._make_client(handler, temperature=0.9).chat(
            [{"role": "user", "content": "hi"}], temperature=0.1, max_tokens=256
        )

    @pytest.mark.asyncio
    async def test_metadata_stripped_from_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "metadata" not in body
            return _chat_response()

        await self._make_client(handler).chat(
            [{"role": "user", "content": "hi"}], metadata={"llm_override": {}}
        )

    @pytest.mark.asyncio
    async def test_metadata_default_stripped_from_generate_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "metadata" not in body
            return _completions_response()

        await self._make_client(handler, metadata={"llm_override": {}}).generate("hi")

    @pytest.mark.asyncio
    async def test_metadata_default_stripped_from_chat_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "metadata" not in body
            return _chat_response()

        await self._make_client(handler, metadata={"llm_override": {}}).chat(
            [{"role": "user", "content": "hi"}]
        )

    @pytest.mark.asyncio
    async def test_metadata_default_stripped_from_stream_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert "metadata" not in body
            return httpx.Response(200, text="data: [DONE]\n")

        client = self._make_client(handler, metadata={"llm_override": {}})
        lines = [line async for line in client.stream_chat([{"role": "user", "content": "hi"}])]
        assert lines == ["data: [DONE]"]

    def test_endpoint_v1_appended_when_missing(self):
        client = OllamaClient(endpoint="http://ollama:11434", model_name="llama3")
        assert client._endpoint == "http://ollama:11434/v1"

    def test_endpoint_v1_not_doubled(self):
        client = OllamaClient(endpoint="http://ollama:11434/v1", model_name="llama3")
        assert client._endpoint == "http://ollama:11434/v1"

    def test_trailing_slash_stripped(self):
        client = OllamaClient(endpoint="http://ollama:11434/v1/", model_name="llama3")
        assert client._endpoint == "http://ollama:11434/v1"

    def test_no_auth_header_sent_by_default(self):
        client = OllamaClient(endpoint="http://ollama:11434", model_name="llama3")
        assert "Authorization" not in client._client.headers

    @pytest.mark.asyncio
    async def test_aclose(self):
        client = OllamaClient(endpoint="http://ollama:11434", model_name="llama3")
        client._client = AsyncMock()
        await client.aclose()
        client._client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# OllamaEmbedder
# ---------------------------------------------------------------------------


class TestOllamaEmbedder:
    def _make_embedder(self, handler, endpoint="http://ollama:11434", **kwargs):
        embedder = OllamaEmbedder(endpoint=endpoint, model_name="nomic-embed-text", **kwargs)
        embedder._client = httpx.AsyncClient(transport=_make_transport(handler))
        return embedder

    @pytest.mark.asyncio
    async def test_embed_returns_sorted_vectors(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["model"] == "nomic-embed-text"
            assert body["input"] == ["hello", "world"]
            return httpx.Response(
                200,
                json={"data": [{"index": 1, "embedding": [0.3, 0.4]}, {"index": 0, "embedding": [0.1, 0.2]}]},
            )

        result = await self._make_embedder(handler).embed(["hello", "world"])
        assert result == [[0.1, 0.2], [0.3, 0.4]]

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
        assert OllamaEmbedder(endpoint="http://ollama:11434", model_name="m", dimension=768).dimension == 768

    @pytest.mark.asyncio
    async def test_no_truncate_prompt_tokens_in_payload(self):
        """Ollama doesn't support truncate_prompt_tokens — must never be sent."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert "truncate_prompt_tokens" not in json.loads(request.content)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

        await self._make_embedder(handler).embed(["test"])

    @pytest.mark.asyncio
    async def test_embed_connection_error(self):
        async def fail(*a, **kw):
            raise httpx.ConnectError("refused")

        embedder = OllamaEmbedder(endpoint="http://ollama:11434", model_name="nomic-embed-text")
        embedder._client = AsyncMock()
        embedder._client.post = fail
        with pytest.raises(EmbeddingAPIError):
            await embedder.embed(["text"])

    @pytest.mark.asyncio
    async def test_embed_timeout(self):
        async def fail(*a, **kw):
            raise httpx.TimeoutException("timeout")

        embedder = OllamaEmbedder(endpoint="http://ollama:11434", model_name="nomic-embed-text")
        embedder._client = AsyncMock()
        embedder._client.post = fail
        with pytest.raises(EmbeddingAPIError):
            await embedder.embed(["text"])

    @pytest.mark.asyncio
    async def test_embed_http_error_raises_embedding_api_error(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="service unavailable")

        with pytest.raises(EmbeddingAPIError):
            await self._make_embedder(handler).embed(["text"])

    @pytest.mark.asyncio
    async def test_embed_http_error_truncates_response_body(self):
        body = "secret-token " + ("x" * 1000)

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text=body)

        with pytest.raises(EmbeddingAPIError) as exc_info:
            await self._make_embedder(handler).embed(["text"])

        error = exc_info.value.extra["error"]
        assert len(error) < len(body)
        assert error.endswith("...(truncated)")

    @pytest.mark.asyncio
    async def test_embed_bad_response_format(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"wrong": "shape"})

        with pytest.raises(EmbeddingResponseError):
            await self._make_embedder(handler).embed(["text"])

    @pytest.mark.asyncio
    async def test_embed_single_empty_response_raises_embedding_response_error(self):
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        with pytest.raises(EmbeddingResponseError, match="Empty Ollama embedding response"):
            await self._make_embedder(handler).embed_single("text")

    def test_endpoint_v1_appended_when_missing(self):
        embedder = OllamaEmbedder(endpoint="http://ollama:11434", model_name="m")
        assert embedder._endpoint == "http://ollama:11434/v1"

    def test_endpoint_v1_not_doubled(self):
        embedder = OllamaEmbedder(endpoint="http://ollama:11434/v1", model_name="m")
        assert embedder._endpoint == "http://ollama:11434/v1"

    @pytest.mark.asyncio
    async def test_aclose(self):
        embedder = OllamaEmbedder(endpoint="http://ollama:11434", model_name="m")
        embedder._client = AsyncMock()
        await embedder.aclose()
        embedder._client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    def test_llm_registered(self):
        from core.llm import llm_registry

        assert "ollama" in llm_registry

    def test_embedder_registered(self):
        from core.embeddings import embedder_registry

        assert "ollama" in embedder_registry
