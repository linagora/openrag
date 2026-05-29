"""Reranker inference clients.

Two classes — Infinity and OpenAI-compatible — both implementing the
``Reranker`` ABC.  Both talk to a ``/rerank`` endpoint with the same
payload shape, differing only in the base URL and transport library
the old code used.  Now both use ``httpx`` directly.
"""

from __future__ import annotations

import httpx
from core.rerankers import Reranker, reranker_registry
from core.utils.exceptions import InferenceConnectionError, InferenceTimeoutError
from core.utils.logging import get_logger

from ._circuit_breaker import with_circuit_breaker
from ._retry import with_retry

logger = get_logger()


@reranker_registry.register("infinity")
class InfinityReranker(Reranker):
    """Reranker backed by an Infinity server."""

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        api_key: str = "",
        timeout: float = 30.0,
        **_kwargs,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._model = model_name
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)

    @with_circuit_breaker("reranker")
    @with_retry(max_attempts=2)
    async def rerank(self, query: str, documents: list[str], top_k: int | None = None) -> list[tuple[int, float]]:
        top_k = min(top_k, len(documents)) if top_k is not None else len(documents)
        try:
            resp = await self._client.post(
                f"{self._endpoint}/rerank",
                json={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_k,
                    "return_documents": False,
                    "raw_scores": True,
                },
            )
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach reranker at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"Reranker request timed out at {self._endpoint}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceConnectionError(
                f"Reranker at {self._endpoint} returned HTTP {exc.response.status_code}"
            ) from exc
        try:
            results = resp.json()["results"]
            return [(r["index"], r["relevance_score"]) for r in results]
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceConnectionError(f"Unexpected reranker response format from {self._endpoint}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


@reranker_registry.register("openai")
class OpenAIReranker(Reranker):
    """Reranker backed by an OpenAI-compatible reranking endpoint."""

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        *,
        api_key: str = "",
        timeout: float = 30.0,
        **_kwargs,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._model = model_name
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)

    @with_circuit_breaker("reranker")
    @with_retry(max_attempts=2)
    async def rerank(self, query: str, documents: list[str], top_k: int | None = None) -> list[tuple[int, float]]:
        top_k = min(top_k, len(documents)) if top_k is not None else len(documents)
        try:
            resp = await self._client.post(
                f"{self._endpoint}/rerank",
                json={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_k,
                },
            )
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach reranker at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"Reranker request timed out at {self._endpoint}") from exc
        except httpx.HTTPStatusError as exc:
            raise InferenceConnectionError(
                f"Reranker at {self._endpoint} returned HTTP {exc.response.status_code}"
            ) from exc
        try:
            results = resp.json()["results"]
            return [(r["index"], r["relevance_score"]) for r in results]
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceConnectionError(f"Unexpected reranker response format from {self._endpoint}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
