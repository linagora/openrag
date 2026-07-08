"""Chunk repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ChunkRepository(ABC):
    """Bulk CRUD operations for text chunks."""

    @abstractmethod
    async def bulk_insert(self, chunks: list[dict]) -> int:
        """Insert multiple chunks. Returns count of inserted rows."""
        ...

    @abstractmethod
    async def get_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        """Batch fetch chunks by IDs."""
        ...

    @abstractmethod
    async def get_by_document_id(self, document_id: str) -> list[dict]:
        """Fetch all chunks for a document, ordered by chunk_index."""
        ...

    @abstractmethod
    async def delete_by_document_id(self, document_id: str) -> int:
        """Delete all chunks belonging to a document. Returns count."""
        ...

    @abstractmethod
    async def delete_by_partition(self, partition: str) -> int:
        """Delete all chunks in a partition. Returns count."""
        ...

    @abstractmethod
    async def bm25_search(self, query_text: str, partition: str, top_k: int = 20) -> list[dict]:
        """Full-text search using tsvector column with ts_rank scoring."""
        ...
