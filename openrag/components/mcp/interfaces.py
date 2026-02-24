from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SearchRequest:
    query: str
    partitions: list[str]
    top_k: int = 5
    similarity_threshold: float = 0.8
    file_id: str | None = None


@dataclass(slots=True)
class SearchResult:
    content: str
    metadata: dict[str, Any]


class BaseSearchGateway(ABC):
    @abstractmethod
    async def search(self, request: SearchRequest) -> list[SearchResult]:
        pass
