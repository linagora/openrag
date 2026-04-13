from abc import ABC, abstractmethod
from typing import Any


class Reranker(ABC):
    """Abstract interface for document reranking.

    Implementations: Reranker (components/reranker.py)
    """

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[Any],
        top_k: int | None = None,
    ) -> list[Any]:
        """Rerank documents by relevance to query.

        Args:
            query: The search query.
            documents: Documents to rerank (implementation defines the type).
            top_k: Maximum number of results to return. None means all.

        Returns:
            Reordered documents with relevance_score set in metadata.
        """
        ...
