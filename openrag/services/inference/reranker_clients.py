"""Reranker inference clients.

Three classes — Infinity, OpenAI-compatible, and Hugging Face Text
Embeddings Inference (TEI) — all implementing the ``Reranker`` ABC and
talking to a ``/rerank`` endpoint, but with different request/response
shapes (see each class' docstring).
"""

from __future__ import annotations

import asyncio
from typing import NoReturn

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
    status code alone is undiagnosable, so the truncated body is logged.
    Best-effort: never let logging/formatting mask the original failure."""
    try:
        body = response.text.strip().replace("\n", " ")
    except Exception:  # noqa: BLE001 — body may be unreadable; the status still helps
        return ""
    if not body:
        return ""
    return body[:limit] + ("…" if len(body) > limit else "")


def _raise_reranker_http_error(endpoint: str, exc: httpx.HTTPStatusError) -> NoReturn:
    """Map a non-2xx reranker reply to ``InferenceConnectionError``.

    The response body is logged for operators but deliberately kept out of the
    raised message: ``InferenceConnectionError.message`` reaches API clients
    verbatim (SSE error events, 503 ``detail``), and an upstream error body can
    carry internals — stack traces, proxy pages, hostnames — that must not leak."""
    status = exc.response.status_code
    detail = _response_detail(exc.response)
    logger.bind(endpoint=endpoint, status=status, body=detail).error("Reranker returned HTTP error")
    raise InferenceConnectionError(f"Reranker at {endpoint} returned HTTP {status}") from exc


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
            _raise_reranker_http_error(self._endpoint, exc)
        try:
            results = resp.json()["results"]
            return [(r["index"], r["relevance_score"]) for r in results]
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceConnectionError(f"Unexpected reranker response format from {self._endpoint}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


# TEI enforces ``--max-client-batch-size`` (default 32) on the number of texts
# per /rerank request and rejects larger requests with HTTP 413. Retrieval
# routinely reranks more candidates than that (retriever top_k defaults to 50),
# so requests are split client-side and merged.
_TEI_MAX_BATCH = 32


@reranker_registry.register("tei")
class TEIReranker(Reranker):
    """Reranker backed by a Hugging Face Text Embeddings Inference (TEI) server.

    TEI's ``/rerank`` differs from Infinity/OpenAI-compatible servers: the
    request field is ``texts`` (not ``documents``), there is no ``model`` or
    ``top_n`` field (a TEI instance serves one fixed model and always scores
    every input), and the response is a bare JSON array of
    ``{"index": ..., "score": ...}`` — not ``{"results": [...]}``.

    Documents are sent in batches of ``_TEI_MAX_BATCH`` (concurrently) because
    TEI caps texts-per-request, and ``truncate: true`` is requested so one
    over-long text degrades to a truncated score instead of failing the whole
    request (Infinity truncates silently; TEI without it rejects the batch).
    Results are merged and re-sorted client-side to honor the ``Reranker``
    ABC's score-descending ordering guarantee.
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
        offsets = range(0, len(documents), _TEI_MAX_BATCH)
        batches = await asyncio.gather(
            *(self._rerank_batch(query, documents[offset : offset + _TEI_MAX_BATCH], offset) for offset in offsets)
        )
        scored = [pair for batch in batches for pair in batch]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k] if top_k is not None else scored

    async def _rerank_batch(self, query: str, texts: list[str], offset: int) -> list[tuple[int, float]]:
        """Score one ≤``_TEI_MAX_BATCH`` slice; indices are shifted back to the full list."""
        try:
            resp = await self._client.post(
                f"{self._endpoint}/rerank",
                json={"query": query, "texts": texts, "raw_scores": False, "truncate": True},
            )
            resp.raise_for_status()
        except httpx.ConnectError as exc:
            raise InferenceConnectionError(f"Cannot reach reranker at {self._endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise InferenceTimeoutError(f"Reranker request timed out at {self._endpoint}") from exc
        except httpx.HTTPStatusError as exc:
            _raise_reranker_http_error(self._endpoint, exc)
        try:
            return [(r["index"] + offset, r["score"]) for r in resp.json()]
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
            _raise_reranker_http_error(self._endpoint, exc)
        try:
            results = resp.json()["results"]
            return [(r["index"], r["relevance_score"]) for r in results]
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceConnectionError(f"Unexpected reranker response format from {self._endpoint}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
