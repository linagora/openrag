from abc import ABC, abstractmethod
from typing import Any


class Retriever(ABC):
    """Abstract interface for document retrieval strategies.

    Implementations: ABCRetriever subclasses (components/retriever.py)
    """

    @abstractmethod
    async def retrieve(self, partition: list[str], query: str) -> list[Any]:
        """Retrieve relevant documents for a query.

        Args:
            partition: List of partition names to search.
            query: The search query.

        Returns:
            List of matching documents.
        """
        ...

    async def expand_search_results(self, results: list[Any]) -> list[Any]:
        """Optionally expand results with related/ancestor documents.

        Default implementation returns results unchanged.
        """
        return results
