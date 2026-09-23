"""Abstract vector store interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from core.models.chunk import Chunk

if TYPE_CHECKING:
    from datetime import datetime


class VectorStore(ABC):
    """Base class for vector database backends."""

    @abstractmethod
    def iter_chunk_metadata(
        self, collection: str, *, partition: str, file_ids: list[str] | None = None, batch_size: int = 500
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Stream scalar-only pages for reconciliation; never load a corpus into memory."""
        raise NotImplementedError

    @abstractmethod
    async def upsert(
        self,
        chunks: list[Chunk],
        collection: str = "default",
        *,
        indexed_at: datetime | None = None,
        vector_field: str | None = None,
    ) -> int:
        """Insert or update chunks. Returns count of upserted items.

        ``indexed_at`` optionally pins the indexation timestamp stamped on the
        chunks so it can match the catalog row; ``None`` means "use now".

        ``vector_field`` is the dense field of the embedder that produced the
        embeddings; a missing one is an error. Callers make sure it exists with
        :meth:`ensure_vector_field`.
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
        vector_field: str | None = None,
    ) -> list[dict[str, Any]]:
        """Similarity search returning raw result dicts.

        ``vector_field`` is the dense field of the query's embedder; a missing
        one is an error. Rows with no value in that field are not returned, and
        a field that does not exist yet returns nothing.

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
        """Create collection if it doesn't exist.

        A fresh collection is created with the dense field named by the
        ``vector_field`` keyword, sized to ``dimension``; both are ignored when
        the collection exists.
        """
        ...

    @abstractmethod
    async def drop_collection(self, name: str) -> None:
        """Drop a collection entirely."""
        ...

    @abstractmethod
    async def ensure_vector_field(self, field: str, dimension: int) -> bool:
        """Make ``field`` exist, be indexed, and be searchable. Idempotent.

        Added to a live collection without disturbing the fields already in
        it. The field is nullable, searches on it skip rows where it is null,
        and it is indexed like every other dense field. ``dimension`` only
        sizes a new field. Returns whether this call created the field.

        Raises:
            ValueError: the backend cannot hold another dense field, or
                ``field`` exists with another dimension.
        """
        ...

    @abstractmethod
    async def drop_vector_field(self, field: str) -> bool:
        """Remove a deleted embedder's dense field, and every vector in it.

        The caller guarantees no partition still uses it. Returns whether this
        call dropped the field, ``False`` when it was already gone.

        Raises:
            ValueError: ``field`` is not a per-embedder dense field, or it is
                the collection's only vector field, which the backend cannot
                drop.
        """
        ...

    @abstractmethod
    async def vector_dimension(self, vector_field: str | None = None) -> int | None:
        """Dimension the live collection actually stores for ``vector_field``.

        ``None`` when it cannot be established — no field given, nothing
        indexed with it yet, or the backend can't be reached. Callers that need
        a number to size buffers should pick their own fallback; callers that
        *report* the dimension must pass the ``None`` through rather than
        substitute a guess.
        """
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
