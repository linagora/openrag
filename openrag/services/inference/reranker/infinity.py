import asyncio

from infinity_client import Client
from infinity_client.api.default import rerank
from infinity_client.models import RerankInput, ReRankResult
from langchain_core.documents.base import Document
from utils.logger import get_logger

from .base import BaseReranker

logger = get_logger()


class InfinityReranker(BaseReranker):
    def __init__(self, config):
        self.model_name = config.reranker.model_name
        self.client = Client(
            base_url=config.reranker.base_url,
            timeout=config.reranker.timeout,
            headers={"Authorization": f"Bearer {config.reranker.api_key}"},
        )
        self.semaphore = asyncio.Semaphore(config.reranker.semaphore)
        logger.debug("Reranker initialized", model_name=self.model_name)

    async def rerank(self, query: str, documents: list[Document], top_k: int | None = None) -> list[Document]:
        async with self.semaphore:
            logger.debug("Reranking documents", documents_count=len(documents), top_k=top_k)
            top_k = min(top_k, len(documents)) if top_k is not None else len(documents)
            rerank_input = RerankInput.from_dict(
                {
                    "model": self.model_name,
                    "query": query,
                    "documents": [doc.page_content for doc in documents],
                    "top_n": top_k,
                    "return_documents": True,
                    "raw_scores": True,  # Normalized score between 0 and 1
                }
            )
            try:
                rerank_result: ReRankResult = await rerank.asyncio(client=self.client, body=rerank_input)
                output = []
                for rerank_res in rerank_result.results:
                    doc = documents[rerank_res.index]
                    doc.metadata["relevance_score"] = rerank_res.relevance_score
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
