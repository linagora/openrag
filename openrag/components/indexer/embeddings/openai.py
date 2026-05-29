"""Backward-compatibility shim — delegates to services.inference.vllm_client.

All new code should import directly from ``services.inference.vllm_client``.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import openai
from core.config.endpoints import EmbedderConfig
from core.utils.exceptions import (
    EmbeddingAPIError,
    EmbeddingResponseError,
    UnexpectedEmbeddingError,
)
from core.utils.logging import get_logger
from langchain_core.documents.base import Document
from openai import OpenAI
from services.inference.vllm_client import VLLMEmbedder  # noqa: F401

from .base import BaseEmbedding

logger = get_logger()


_SYNC_POOL = ThreadPoolExecutor(max_workers=1)


def _run_sync(coro):
    """Run an async coroutine from sync code, safe inside a running event loop (e.g. Ray)."""
    return _SYNC_POOL.submit(asyncio.run, coro).result()


def _normalize_texts(texts: list[str | Document]) -> list[str]:
    return [item.page_content if isinstance(item, Document) else item for item in texts]


class _ShimOpenAIEmbedding(BaseEmbedding):
    """Legacy shim — delegates to ``VLLMEmbedder`` for actual HTTP transport.

    Preserves the sync ``embed_documents``/``embed_query`` contract expected by
    ``vectordb.py`` (via LangChain's ``aembed_documents`` thread wrapper) while
    using VLLMEmbedder's long-lived async httpx pool under the hood.
    """

    def __init__(self, embeddings_config: EmbedderConfig):
        self._delegate = VLLMEmbedder(
            endpoint=embeddings_config.base_url,
            model_name=embeddings_config.model_name,
            max_model_len=embeddings_config.max_model_len,
            api_key=embeddings_config.api_key,
        )

    @property
    def embedding_dimension(self) -> int:
        # Probe once if unknown — legacy callers (e.g. MilvusDB schema creation) read
        # this before any embed() call, but VLLMEmbedder only learns its dimension
        # from a real response. The probe must run on a one-off sync httpx.Client:
        # asyncio.run() here would tear down the loop and leave the delegate's
        # long-lived AsyncClient pool with stale connections, breaking the next
        # real async call with "Event loop is closed".
        try:
            return self._delegate.dimension
        except RuntimeError:
            pass
        body: dict = {"model": self._delegate._model, "input": ["dim-probe"]}
        if self._delegate._max_model_len is not None:
            body["truncate_prompt_tokens"] = self._delegate._max_model_len
        with httpx.Client(timeout=30.0, headers=dict(self._delegate._client.headers)) as client:
            resp = client.post(f"{self._delegate._endpoint}/embeddings", json=body)
            resp.raise_for_status()
        self._delegate._dimension = len(resp.json()["data"][0]["embedding"])
        return self._delegate._dimension

    def embed_documents(self, texts: list[str | Document]) -> list[list[float]]:
        if not texts:
            return []
        return _run_sync(self._delegate.embed(_normalize_texts(texts)))

    async def aembed_documents(self, texts: list[str | Document]) -> list[list[float]]:
        if not texts:
            return []
        return await self._delegate.embed(_normalize_texts(texts))

    def embed_query(self, text: str) -> list[float]:
        return _run_sync(self._delegate.embed_single(text))

    async def aembed_query(self, text: str) -> list[float]:
        return await self._delegate.embed_single(text)


class OpenAIEmbedding(BaseEmbedding):
    """Legacy OpenAI embedding wrapper. New code should use VLLMEmbedder (via DI)."""

    def __init__(self, embeddings_config):
        self.embedding_model = embeddings_config.model_name
        self.base_url = embeddings_config.base_url
        self.api_key = embeddings_config.api_key
        self.max_model_len = embeddings_config.max_model_len
        self._sync_client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    @property
    def embedding_dimension(self) -> int:
        try:
            output = self.embed_documents([Document(page_content="test")])
            return len(output[0])
        except Exception:
            raise

    def embed_documents(self, texts: list[str | Document]) -> list[list[float]]:
        if isinstance(texts[0], Document):
            texts = [doc.page_content for doc in texts]

        try:
            response = self._sync_client.embeddings.create(
                model=self.embedding_model,
                input=texts,
                extra_body={"truncate_prompt_tokens": self.max_model_len},
            )
            return [vector.embedding for vector in response.data]

        except openai.APIError as e:
            logger.error("API error in embed_documents", error=str(e))
            raise EmbeddingAPIError(
                f"OpenAI API error during document embedding: {e!s}",
                model_name=self.embedding_model,
                base_url=self.base_url,
                error=str(e),
            )

        except (IndexError, AttributeError) as e:
            logger.error("Error while accessing embedding data", error=str(e))
            raise EmbeddingResponseError(
                "Failed to retrieve document embeddings due to unexpected response format.",
                model_name=self.embedding_model,
                base_url=self.base_url,
                error=str(e),
            )

        except Exception as e:
            logger.exception("Unexpected error while embedding documents", error=str(e))
            raise UnexpectedEmbeddingError(
                f"Failed to embed documents: {e!s}",
                model_name=self.embedding_model,
                base_url=self.base_url,
                error=str(e),
            )

    def embed_query(self, text: str) -> list[float]:
        try:
            output = self.embed_documents([Document(page_content=text)])
            return output[0]
        except Exception:
            raise
