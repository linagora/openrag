import asyncio

import httpx
from langchain_core.documents.base import Document
from utils.logger import get_logger

from .base import BaseReranker

logger = get_logger()


class OpenAIReranker(BaseReranker):
    def __init__(self, config):
        self.model_name = config.reranker.model_name
        base_url = config.reranker.base_url.rstrip("/")
        self.rerank_url = f"{base_url}/rerank"
        self.semaphore = asyncio.Semaphore(config.reranker.semaphore)
        self.timeout = config.reranker.timeout
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.reranker.api_key}"},
        )
        logger.debug("OpenAI Reranker initialized", model_name=self.model_name)

    async def rerank(self, query: str, documents: list[Document], top_k: int | None = None) -> list[Document]:
        async with self.semaphore:
            logger.debug("Reranking documents", documents_count=len(documents), top_k=top_k)
            top_k = min(top_k, len(documents)) if top_k is not None else len(documents)
            try:
                response = await self.client.post(
                    self.rerank_url,
                    json={
                        "model": self.model_name,
                        "query": query,
                        "documents": [doc.page_content for doc in documents],
                        "top_n": top_k,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()

                output = []
                for result in data["results"]:
                    doc = documents[result["index"]]
                    doc.metadata["relevance_score"] = result["relevance_score"]
                    output.append(doc)
                return output

            except Exception as e:
                logger.error(
                    "Reranking failed",
                    error=str(e),
                    model_name=self.model_name,
                    documents_count=len(documents),
                )
                return documents[:top_k]
