"""vLLM / OpenAI-compatible inference clients.

Three classes grouped by server — all talk to the same OpenAI-compatible API:

* ``VLLMClient``   → ``LLM``      (chat completions)
* ``VLLMEmbedder`` → ``Embedder``  (embeddings)
* ``VLLMVision``   → ``VLM``       (image captioning via chat completions)

Each class has its own circuit breaker instance so an embedder outage
doesn't trip the LLM breaker.
"""

from __future__ import annotations

import asyncio
import base64
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
from core.utils.logging import get_logger
from core.vlm import VLM, vlm_registry
from tqdm.asyncio import tqdm

from ._circuit_breaker import with_circuit_breaker
from ._retry import with_retry

logger = get_logger()


def _parse_response(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError as e:
        raise InferenceError(f"Invalid JSON from inference server ({resp.url}): {e}", status_code=502) from e


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


@llm_registry.register("vllm")
class VLLMClient(LLM):
    """OpenAI-compatible LLM client backed by vLLM.

    *endpoint* should include the version prefix, e.g. ``http://vllm:8000/v1``.
    A single long-lived ``httpx.AsyncClient`` is reused across requests for
    connection pooling.
    """

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        api_key: str = "",
        timeout: float = 240.0,
        **kwargs,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model_name
        self._api_key = api_key
        self._defaults: dict = kwargs
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)

    def _resolve_overrides(self, kwargs: dict) -> tuple[str, str, dict[str, str] | None]:
        """Read ``metadata.llm_override`` from *kwargs* without mutating caller data.

        Pure read: ``kwargs`` is untouched so retries see the original override on
        every attempt. The caller strips ``metadata`` from the outbound payload via
        ``_payload_kwargs`` — every ``metadata`` key is OpenRAG-internal and never
        belongs on the wire.
        """
        base_url = self._endpoint
        model = self._model
        override_headers: dict[str, str] | None = None

        llm_override = (kwargs.get("metadata") or {}).get("llm_override") or {}
        if llm_override:
            if llm_override.get("base_url"):
                base_url = llm_override["base_url"].rstrip("/")
            if llm_override.get("model"):
                model = llm_override["model"]
            if llm_override.get("api_key"):
                override_headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {llm_override['api_key']}",
                }

        return base_url, model, override_headers

    @with_circuit_breaker("llm")
    @with_retry(max_attempts=3)
    async def generate(self, prompt: str, **kwargs) -> dict:
        base_url, model, headers = self._resolve_overrides(kwargs)
        kwargs.pop("metadata", None)
        payload = {**self._defaults, **kwargs, "model": model, "prompt": prompt}
        try:
            resp = await self._client.post(f"{base_url}/completions", json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach LLM at {base_url}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"LLM request timed out at {base_url}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"LLM error ({exc.response.status_code}): {exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        return _parse_response(resp)

    @with_circuit_breaker("llm")
    @with_retry(max_attempts=3)
    async def chat(self, messages: list[dict[str, str]], **kwargs) -> dict:
        base_url, model, headers = self._resolve_overrides(kwargs)
        kwargs.pop("metadata", None)
        payload = {**self._defaults, **kwargs, "model": model, "messages": messages, "stream": False}
        try:
            resp = await self._client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach LLM at {base_url}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"LLM request timed out at {base_url}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"LLM error ({exc.response.status_code}): {exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        return _parse_response(resp)

    async def stream_chat(self, messages: list[dict[str, str]], **kwargs) -> AsyncIterator[str]:
        base_url, model, headers = self._resolve_overrides(kwargs)
        kwargs.pop("metadata", None)
        payload = {**self._defaults, **kwargs, "model": model, "messages": messages, "stream": True}
        try:
            async with self._client.stream(
                "POST", f"{base_url}/chat/completions", json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise InferenceError(
                        f"LLM streaming error ({resp.status_code}): {resp.text[:500]}",
                        status_code=resp.status_code,
                    )
                async for line in resp.aiter_lines():
                    yield line
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach LLM at {base_url}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"LLM streaming request timed out at {base_url}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


@embedder_registry.register("vllm")
class VLLMEmbedder(Embedder):
    """OpenAI-compatible embedding client backed by vLLM.

    Replaces the sync ``openai.OpenAI`` SDK with an async ``httpx`` client.
    """

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        max_model_len: int | None = None,
        dimension: int | None = None,
        timeout: float = 120.0,
        batch_size: int = 64,
        embed_concurrency: int = 4,
        api_key: str = "",
        **_kwargs,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model_name
        self._max_model_len = max_model_len
        self._dimension: int | None = dimension
        # Big documents produce thousands of chunks; sending them in one request
        # overruns the endpoint's time budget. Split into `batch_size` slices and
        # run at most `embed_concurrency` of them at once to bound remote load.
        self._batch_size = max(1, batch_size)
        self._embed_concurrency = max(1, embed_concurrency)
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts*, splitting large inputs into bounded-concurrent batches.

        A single request for thousands of chunks (e.g. a big PDF) overruns the
        embedder's time budget and trips the client timeout. We slice the input
        into ``batch_size`` chunks and run at most ``embed_concurrency`` requests
        at once, then concatenate the vectors back in input order.
        """
        if not texts:
            return []
        if len(texts) <= self._batch_size:
            return await self._embed_batch(texts)

        batches = [texts[i : i + self._batch_size] for i in range(0, len(texts), self._batch_size)]
        semaphore = asyncio.Semaphore(self._embed_concurrency)

        async def _run(batch: list[str]) -> list[list[float]]:
            async with semaphore:
                return await self._embed_batch(batch)

        # Explicit tasks so that when one batch fails we can cancel the rest.
        # tqdm.gather (like asyncio.gather) propagates the first exception but
        # leaves the other in-flight batches running, where they would keep
        # consuming embedder capacity after embed() has already failed.
        tasks = [asyncio.ensure_future(_run(batch)) for batch in batches]
        try:
            results = await tqdm.gather(
                *tasks,
                desc=f"Embedding {len(texts)} chunks ({len(batches)} batches of {self._batch_size})",
            )
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        vectors: list[list[float]] = []
        for batch_vectors in results:
            vectors.extend(batch_vectors)
        return vectors

    @with_circuit_breaker("embedder")
    @with_retry(max_attempts=3)
    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        body: dict = {"model": self._model, "input": texts}
        if self._max_model_len is not None:
            body["truncate_prompt_tokens"] = self._max_model_len
        try:
            resp = await self._client.post(f"{self._endpoint}/embeddings", json=body)
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise EmbeddingAPIError(
                f"Cannot reach embedder at {self._endpoint}",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc
        except httpx.TimeoutException as exc:
            raise EmbeddingAPIError(
                f"Embedder request timed out at {self._endpoint}",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise EmbeddingAPIError(
                f"Embedder API error ({exc.response.status_code})",
                model_name=self._model,
                base_url=self._endpoint,
                error=exc.response.text,
            ) from exc

        try:
            data = resp.json()["data"]
            embeddings = [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise EmbeddingResponseError(
                "Unexpected embedding response format",
                model_name=self._model,
                base_url=self._endpoint,
                error=str(exc),
            ) from exc

        if self._dimension is None and embeddings:
            self._dimension = len(embeddings[0])
        return embeddings

    async def embed_single(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            raise RuntimeError("Embedding dimension unknown — call embed() first")
        return self._dimension

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# VLM (Vision-Language Model)
# ---------------------------------------------------------------------------


@vlm_registry.register("vllm")
class VLLMVision(VLLMClient, VLM):
    """OpenAI-compatible vision client backed by vLLM.

    Inherits connection pooling, retry, and circuit breaker from VLLMClient.
    Adds image captioning via the same OpenAI-compatible chat/completions endpoint.
    """

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        timeout: float = 60.0,
        api_key: str = "",
        max_tokens: int = 1024,
        **kwargs,
    ) -> None:
        super().__init__(endpoint=endpoint, model_name=model_name, api_key=api_key, timeout=timeout, **kwargs)
        self._max_tokens = max_tokens

    @with_circuit_breaker("vlm")
    @with_retry(max_attempts=2)
    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        image_b64 = base64.b64encode(image_bytes).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                    {
                        "type": "text",
                        "text": prompt or "Describe this image in detail.",
                    },
                ],
            }
        ]
        try:
            resp = await self._client.post(
                f"{self._endpoint}/chat/completions",
                json={"model": self._model, "messages": messages, "max_tokens": self._max_tokens},
            )
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach VLM at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"VLM request timed out at {self._endpoint}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceError(
                f"VLM error ({exc.response.status_code}): {exc.response.text[:500]}",
                status_code=exc.response.status_code,
            ) from exc
        return _parse_response(resp)["choices"][0]["message"]["content"]

    async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
        return list(await asyncio.gather(*(self.caption_image(img, prompt) for img in images)))
