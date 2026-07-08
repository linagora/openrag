"""Transitional port for chunk-level retrieval operations.

The strict ``VectorStore`` ABC in ``core/vector_stores`` is intentionally
narrow — ``search(embedding, top_k, ...)``. Phase 5 retrievers, however,
still go through the legacy Milvus Ray actor which exposes higher-level
operations:

  * search by query string (embedding done internally, plus BM25)
  * multi-query search (one round per query, dedup at the bottom)
  * related-chunk lookup by ``relationship_id``
  * ancestor lookup by ``file_id`` with depth bound

Defining these on a dedicated port lets the retriever depend on a clean
interface from day one, while a small shim in ``services/storage/``
adapts the Ray actor to it. When the god object is decomposed in Phase 7
this port either retires (operations move to ``VectorStore`` +
``ChunkRepository``) or evolves into the shape MilvusVectorStore exposes
directly.

Returns are domain ``Chunk`` objects throughout — no LangChain types
leak across this boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.chunk import Chunk


class RetrievalSearcher(ABC):
    """Operations a retriever needs from the chunk store."""

    @abstractmethod
    async def search(
        self,
        query: str,
        partition: list[str],
        top_k: int,
        filter: str | None = None,
        filter_params: dict | None = None,
        similarity_threshold: float = 0.0,
        with_surrounding_chunks: bool = True,
    ) -> list[Chunk]:
        """Single-query similarity search."""
        ...

    @abstractmethod
    async def multi_query_search(
        self,
        queries: list[str],
        partition: list[str],
        top_k_per_query: int,
        filter: str | None = None,
        filter_params: dict | None = None,
        similarity_threshold: float = 0.0,
        with_surrounding_chunks: bool = True,
    ) -> list[Chunk]:
        """Run one similarity search per query, return the merged result."""
        ...

    @abstractmethod
    async def get_related_chunks(
        self,
        partition: str,
        relationship_id: str,
        limit: int,
    ) -> list[Chunk]:
        """Fetch other chunks belonging to the same relationship group."""
        ...

    @abstractmethod
    async def get_ancestor_chunks(
        self,
        partition: str,
        file_id: str,
        limit: int,
        max_ancestor_depth: int | None = None,
    ) -> list[Chunk]:
        """Walk parent links up the document tree from a file."""
        ...
