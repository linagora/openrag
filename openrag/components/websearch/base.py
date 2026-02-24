from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    display_url: str | None = None
    hostname: str | None = None


class BaseWebSearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str) -> list[WebResult]:
        """Return web results for the query. May raise on failure."""
        ...
