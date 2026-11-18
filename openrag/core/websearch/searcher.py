"""The web-search port: what the chat needs from a web search provider."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    content: str | None = None


class WebSearcher(ABC):
    """A web search provider (``services/websearch/providers/``)."""

    @abstractmethod
    async def search(self, query: str) -> list[WebResult]:
        """Return web results for the query. May raise on failure."""
        ...
