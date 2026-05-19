"""Ollama inference clients.

Ollama exposes an OpenAI-compatible ``/v1`` API since v0.1.24, so these
clients are thin wrappers over the vLLM clients with Ollama-specific defaults
and without vLLM-only fields (``truncate_prompt_tokens``).

* ``OllamaClient``   → ``LLM``      (chat completions via /v1/chat/completions)
* ``OllamaEmbedder`` → ``Embedder``  (embeddings via /v1/embeddings)
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from core.embeddings import Embedder, embedder_registry
from core.llm import LLM, llm_registry
from core.utils.exceptions import (
    EmbeddingAPIError,
    EmbeddingResponseError,
    InferenceConnectionError,
    InferenceError,
    InferenceTimeoutError,
)
from utils.logger import get_logger

from ._circuit_breaker import with_circuit_breaker
from ._retry import with_retry
from .vllm_client import _parse_response

logger = get_logger()
_ERROR_SNIPPET_LIMIT = 500


def _error_snippet(text: str) -> str:
    snippet = " ".join(text.split())[:_ERROR_SNIPPET_LIMIT]
    if len(text) > _ERROR_SNIPPET_LIMIT:
        return f"{snippet}...(truncated)"
    return snippet


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


@llm_registry.register("ollama")
class OllamaClient(LLM):
    """Ollama LLM client using the OpenAI-compatible /v1 API.

    *endpoint* should point to the Ollama server root or include the ``/v1``
    prefix, e.g. ``http://localhost:11434/v1``.
    """

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        timeout: float = 240.0,
        **kwargs,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        if not self._endpoint.endswith("/v1"):
            self._endpoint = f"{self._endpoint}/v1"
        self._model = model_name
        self._defaults: dict = kwargs
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    @with_circuit_breaker("llm")
    @with_retry(max_attempts=3)
    async def generate(self, prompt: str, **kwargs) -> dict:
        payload = {**self._defaults, **kwargs, "model": self._model, "prompt": prompt}
        payload.pop("metadata", None)
        try:
            resp = await self._client.post(f"{self._endpoint}/completions", json=payload)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach Ollama at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"Ollama request timed out at {self._endpoint}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"Ollama error ({exc.response.status_code}): {exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        return _parse_response(resp)

    @with_circuit_breaker("llm")
    @with_retry(max_attempts=3)
    async def chat(self, messages: list[dict[str, str]], **kwargs) -> dict:
        payload = {**self._defaults, **kwargs, "model": self._model, "messages": messages, "stream": False}
        payload.pop("metadata", None)
        try:
            resp = await self._client.post(f"{self._endpoint}/chat/completions", json=payload)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach Ollama at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"Ollama request timed out at {self._endpoint}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"Ollama error ({exc.response.status_code}): {exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        return _parse_response(resp)

    async def stream_chat(self, messages: list[dict[str, str]], **kwargs) -> AsyncIterator[str]:
        payload = {**self._defaults, **kwargs, "model": self._model, "messages": messages, "stream": True}
        payload.pop("metadata", None)
        try:
            async with self._client.stream(
                "POST", f"{self._endpoint}/chat/completions", json=payload
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise InferenceError(
                        f"Ollama streaming error ({resp.status_code}): {resp.text[:500]}",
                        status_code=resp.status_code,
                    )
                async for line in resp.aiter_lines():
                    yield line
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach Ollama at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"Ollama streaming request timed out at {self._endpoint}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


@embedder_registry.register("ollama")
class OllamaEmbedder(Embedder):
    """Ollama embedding client using the OpenAI-compatible /v1/embeddings API."""

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        dimension: int | None = None,
        timeout: float = 60.0,
        **_kwargs,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        if not self._endpoint.endswith("/v1"):
            self._endpoint = f"{self._endpoint}/v1"
        self._model = model_name
        self._dimension: int | None = dimension
        self._client = httpx.AsyncClient(timeout=timeout)

    @with_circuit_breaker("embedder")
    @with_retry(max_attempts=3)
    async def embed(self, texts: list[str]) -> list[list[float]]:
        body = {"model": self._model, "input": texts}
        try:
            resp = await self._client.post(f"{self._endpoint}/embeddings", json=body)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise EmbeddingAPIError(
                f"Cannot reach Ollama embedder at {self._endpoint}",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc
        except httpx.TimeoutException as exc:
            raise EmbeddingAPIError(
                f"Ollama embedder request timed out at {self._endpoint}",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise EmbeddingAPIError(
                f"Ollama embedder API error ({exc.response.status_code})",
                model_name=self._model,
                base_url=self._endpoint,
                error=_error_snippet(exc.response.text),
            ) from exc

        try:
            data = resp.json()["data"]
            embeddings = [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingResponseError(
                "Unexpected Ollama embedding response format",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc

        if self._dimension is None and embeddings:
            self._dimension = len(embeddings[0])
        return embeddings

    async def embed_single(self, text: str) -> list[float]:
        result = await self.embed([text])
        if not result:
            raise EmbeddingResponseError(
                "Empty Ollama embedding response",
                model_name=self._model,
                base_url=self._endpoint,
                error="No vectors returned",
            )
        return result[0]

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            raise RuntimeError("Embedding dimension unknown — call embed() first")
        return self._dimension

    async def aclose(self) -> None:
        await self._client.aclose()
