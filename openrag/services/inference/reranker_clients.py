"""Reranker inference clients.

Three classes — Infinity, OpenAI-compatible, and Hugging Face Text
Embeddings Inference (TEI) — all implementing the ``Reranker`` ABC and
talking to a ``/rerank`` endpoint, but with different request/response
shapes (see each class' docstring).
"""

from __future__ import annotations

import httpx
from core.rerankers import Reranker, reranker_registry
from core.utils.exceptions import InferenceConnectionError, InferenceTimeoutError
from core.utils.logging import get_logger

from ._circuit_breaker import with_circuit_breaker
from ._retry import with_retry

logger = get_logger()


def _response_detail(response: httpx.Response, limit: int = 500) -> str:
    """Short, safe snippet of an error response body.

    A non-2xx reranker reply (notably a 422 from a pydantic-validated server)
    carries the exact reason — which field/type it rejected — in its body. The
    status code alone is undiagnosable, so surface a truncated body in the raised
    error. Best-effort: never let logging/formatting mask the original failure."""
    try:
        body = response.text.strip().replace("\n", " ")
    except Exception:  # noqa: BLE001 — body may be unreadable; the status still helps
        return ""
    if not body:
        return ""
    return body[:limit] + ("…" if len(body) > limit else "")


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
            detail = _response_detail(exc.response)
            raise InferenceConnectionError(
                f"Reranker at {self._endpoint} returned HTTP {exc.response.status_code}"
                + (f": {detail}" if detail else "")
            ) from exc
        try:
            results = resp.json()["results"]
            return [(r["index"], r["relevance_score"]) for r in results]
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceConnectionError(f"Unexpected reranker response format from {self._endpoint}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


@reranker_registry.register("tei")
class TEIReranker(Reranker):
    """Reranker backed by a Hugging Face Text Embeddings Inference (TEI) server.

    TEI's ``/rerank`` differs from Infinity/OpenAI-compatible servers: the
    request field is ``texts`` (not ``documents``), there is no ``model`` or
    ``top_n`` field (a TEI instance serves one fixed model and always scores
    every input), and the response is a bare JSON array of
    ``{"index": ..., "score": ...}`` — not ``{"results": [...]}``. TEI does
    sort its response by score descending, but we re-sort explicitly to keep
    the ``Reranker`` ABC's ordering guarantee independent of that.
    """

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
        self._model = model_name  # unused: TEI serves a single fixed model, no selector in the request
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)

    @with_circuit_breaker("reranker")
    @with_retry(max_attempts=2)
    async def rerank(self, query: str, documents: list[str], top_k: int | None = None) -> list[tuple[int, float]]:
        try:
            resp = await self._client.post(
                f"{self._endpoint}/rerank",
                json={"query": query, "texts": documents, "raw_scores": False},
            )
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach reranker at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"Reranker request timed out at {self._endpoint}") from exc
        except httpx.HTTPStatusError as exc:
            detail = _response_detail(exc.response)
            raise InferenceConnectionError(
                f"Reranker at {self._endpoint} returned HTTP {exc.response.status_code}"
                + (f": {detail}" if detail else "")
            ) from exc
        try:
            results = sorted(resp.json(), key=lambda r: r["score"], reverse=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceConnectionError(f"Unexpected reranker response format from {self._endpoint}") from exc
        if top_k is not None:
            results = results[:top_k]
        try:
            return [(r["index"], r["score"]) for r in results]
        except (KeyError, TypeError) as exc:
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
            detail = _response_detail(exc.response)
            raise InferenceConnectionError(
                f"Reranker at {self._endpoint} returned HTTP {exc.response.status_code}"
                + (f": {detail}" if detail else "")
            ) from exc
        try:
            results = resp.json()["results"]
            return [(r["index"], r["relevance_score"]) for r in results]
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceConnectionError(f"Unexpected reranker response format from {self._endpoint}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
