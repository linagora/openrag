"""Abstract reranker interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Reranker(ABC):
    """Base class for all reranking providers."""

    @abstractmethod
    async def rerank(self, query: str, documents: list[str], top_k: int | None = None) -> list[tuple[int, float]]:
        """Rerank documents for a query.

        Returns list of (original_index, score) sorted by relevance.
        """
        ...
