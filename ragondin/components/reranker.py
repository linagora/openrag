import asyncio
from infinity_client import Client
from infinity_client.api.default import rerank
from infinity_client.models import RerankInput, ReRankResult
from langchain_core.documents.base import Document


class Reranker:
    def __init__(self, logger, config):
        self.model_name = config.reranker["model_name"]
        self.client = Client(base_url=config.reranker["base_url"])
        self.logger = logger
        self.semaphore = asyncio.Semaphore(
            5
        )  # Only allow 5 reranking operation at a time
        self.logger.debug("Reranker initialized", model_name=self.model_name)

    async def rerank(
        self, query: str, documents: list[Document], top_k: int
    ) -> list[Document]:
        async with self.semaphore:
            self.logger.debug(
                "Reranking documents", documents_count=len(documents), top_k=top_k
            )
            top_k = min(top_k, len(documents))
            rerank_input = RerankInput.from_dict(
                {
                    "model": self.model_name,
                    "query": query,
                    "documents": [doc.page_content for doc in documents],
                    "top_n": top_k,
                    "return_documents": True,
                    "raw_scores": True,
                }
            )
            try:
                rerank_result: ReRankResult = await rerank.asyncio(
                    client=self.client, body=rerank_input
                )
                output = []
                for rerank_res in rerank_result.results:
                    doc = documents[rerank_res.index]
                    doc.metadata["relevance_score"] = rerank_res.relevance_score
                    output.append(doc)
                return output

            except Exception as e:
                self.logger.error(
                    "Reranking failed",
                    error=str(e),
                    model_name=self.model_name,
                    documents_count=len(documents),
                )
                raise e
