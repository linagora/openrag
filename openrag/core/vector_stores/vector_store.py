"""Abstract vector store interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from openrag.core.models.chunk import Chunk

if TYPE_CHECKING:
    from datetime import datetime


class VectorStore(ABC):
    """Base class for vector database backends."""

    @abstractmethod
    async def upsert(
        self,
        chunks: list[Chunk],
        collection: str = "default",
        *,
        indexed_at: datetime | None = None,
    ) -> int:
        """Insert or update chunks. Returns count of upserted items.

        ``indexed_at`` optionally pins the indexation timestamp stamped on the
        chunks so it can match the catalog row; ``None`` means "use now".
        """
        ...

    @abstractmethod
    async def search(
        self,
        embedding: list[float],
        query_text: str | None = None,
        top_k: int = 10,
        collection: str = "default",
        filters: dict[str, Any] | None = None,
        similarity_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Similarity search returning raw result dicts.

        Hybrid (dense + lexical) retrieval is a backend configuration
        concern, not a separate entry point: when a backend has it enabled
        it fuses a dense vector match with a lexical match, and ``query_text``
        carries the raw query such backends compute the sparse vector from
        server-side. Dense-only backends ignore ``query_text``.

        ``similarity_threshold`` (when set) lower-bounds the dense leg's
        similarity; backends supporting range search drop anything scoring at
        or below it. ``None`` disables the bound.
        """
        ...

    @abstractmethod
    async def delete(self, ids: list[str], collection: str = "default") -> int:
        """Delete chunks by ID. Returns count of deleted items."""
        ...

    @abstractmethod
    async def delete_by_filter(self, filters: dict[str, Any]) -> int:
        """Delete chunks matching the given filter expression. Returns count."""
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
