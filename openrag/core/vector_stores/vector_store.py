"""Abstract vector store interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openrag.core.models.chunk import Chunk


class VectorStore(ABC):
    """Base class for vector database backends."""

    @abstractmethod
    async def upsert(self, chunks: list[Chunk], collection: str = "default") -> int:
        """Insert or update chunks. Returns count of upserted items."""
        ...

    @abstractmethod
    async def search(
        self,
        embedding: list[float],
        top_k: int = 10,
        collection: str = "default",
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search by embedding vector. Returns raw results."""
        ...

    @abstractmethod
    async def delete(self, ids: list[str], collection: str = "default") -> int:
        """Delete chunks by ID. Returns count of deleted items."""
        ...

    @abstractmethod
    async def ensure_collection(self, name: str, dimension: int, **kwargs: Any) -> None:
        """Create collection if it doesn't exist."""
        ...

    @abstractmethod
    async def drop_collection(self, name: str) -> None:
        """Drop a collection entirely."""
        ...

    @abstractmethod
    async def collection_exists(self, name: str) -> bool:
        """Check if collection exists."""
        ...

    @abstractmethod
    async def query_ids_by_filter(self, collection: str, filters: dict[str, Any]) -> list[str]:
        """Return chunk IDs matching the given filter expression."""
        ...

    @abstractmethod
    async def query_chunks_by_filter(
        self,
        collection: str,
        filters: dict[str, Any],
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return full chunk data matching the given filter expression."""
        ...
