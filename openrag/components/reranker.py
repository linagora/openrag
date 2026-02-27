import asyncio

from infinity_client import Client
from infinity_client.api.default import rerank
from infinity_client.models import RerankInput, ReRankResult
from langchain_core.documents.base import Document


class BaseReranker:
    async def rerank(self, query: str, documents: list[Document], top_k: int | None = None) -> list[Document]:
        """Rerank a list of documents based on a query and an optional top_k parameter"""
        raise NotImplementedError("Rerank method must be implemented by subclasses")

    @staticmethod
    def rrf_reranking(doc_lists: list[list], k: int = 60) -> list[Document]:
        """Reciprocal_rank_fusion that takes multiple lists of ranked documents
        and an optional parameter k used in the RRF formula
        RRF formula: \\sum_{i=1}^{n} \frac{1}{k + rank_i}
        where rank_i is the rank of the document in the i-th list and n is the number of lists.

        k small: High sensitivity to top ranks
        k large: More balanced sensitivity across ranks
        k = 60 a common and balanced choice in practice.
        """

        if len(doc_lists) == 1:
            return doc_lists[0]

        fused_scores = {}
        for doc_list in doc_lists:
            doc_list: list[Document]
            for rank, doc in enumerate(doc_list, start=1):
                doc_id = doc.metadata.get("_id")

                score, d = fused_scores.get(doc_id, (0, doc))
                fused_scores[doc_id] = (score + (1 / (rank + k)), d)

        # sort the docs
        reranked_docs = [doc for score, doc in sorted(fused_scores.values(), key=lambda x: x[0], reverse=True)]
        return reranked_docs


class Reranker(BaseReranker):
    def __init__(self, logger, config):
        self.model_name = config.reranker["model_name"]
        self.client = Client(base_url=config.reranker["base_url"])
        self.logger = logger
        self.semaphore = asyncio.Semaphore(5)  # Only allow 5 reranking operation at a time
        self.temporal_reranking = config.reranker.get("temporal_reranking", False)
        self.logger.debug("Reranker initialized", model_name=self.model_name)

    async def rerank(self, query: str, documents: list[Document], top_k: int | None = None) -> list[Document]:
        async with self.semaphore:
            self.logger.debug("Reranking documents", documents_count=len(documents), top_k=top_k)
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
                self.logger.error(
                    "Reranking failed",
                    error=str(e),
                    model_name=self.model_name,
                    documents_count=len(documents),
                )
                raise e
