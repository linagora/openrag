from abc import ABC, abstractmethod

from langchain_core.documents.base import Document


class BaseReranker(ABC):
    @abstractmethod
    async def rerank(self, query: str, documents: list[Document], top_k: int | None = None) -> list[Document]:
        """Rerank a list of documents based on a query and an optional top_k parameter"""

    @staticmethod
    def rrf_reranking(doc_lists: list[list[Document]], k: int = 60) -> list[Document]:
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

        # Initialize a dictionary to hold fused scores for each unique document
        fused_scores = {}

        for doc_list in doc_lists:
            doc_list: list[Document]
            for rank, doc in enumerate(doc_list, start=1):
                doc_id = doc.metadata.get("_id")
                doc_key = ("id", doc_id) if doc_id is not None else ("object", id(doc))

                score, d = fused_scores.get(doc_key, (0, doc))
                fused_scores[doc_key] = (score + 1 / (rank + k), d)

        # sort the docs
        reranked_docs = [doc for _, doc in sorted(fused_scores.values(), key=lambda x: x[0], reverse=True)]
        return reranked_docs
