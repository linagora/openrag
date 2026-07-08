"""Stub :class:`ChunkRepository`.

Chunks currently live exclusively in Milvus — the vector store owns the
text, the embedding, and the BM25 sparse index. A Postgres-side chunk
table (with ``tsvector`` for FTS) is a post-refactoring feature: it
would unlock keyword-search routes that don't round-trip through
Milvus, plus easier full-table backups. Until that lands every method
raises :class:`StubRepositoryError`.
"""

from __future__ import annotations

from core.ports.chunk_repo import ChunkRepository
from services.persistence._stubs import _StubRepositoryBase, stub_not_implemented


class PgChunkRepository(_StubRepositoryBase, ChunkRepository):
    """TODO: real impl once the ``chunks`` table is added."""

    async def bulk_insert(self, chunks: list[dict]) -> int:
        raise stub_not_implemented("Postgres-side chunk storage")

    async def get_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        raise stub_not_implemented("Postgres-side chunk storage")

    async def get_by_document_id(self, document_id: str) -> list[dict]:
        raise stub_not_implemented("Postgres-side chunk storage")

    async def delete_by_document_id(self, document_id: str) -> int:
        raise stub_not_implemented("Postgres-side chunk storage")

    async def delete_by_partition(self, partition: str) -> int:
        raise stub_not_implemented("Postgres-side chunk storage")

    async def bm25_search(self, query_text: str, partition: str, top_k: int = 20) -> list[dict]:
        raise stub_not_implemented("Postgres-side BM25 / tsvector FTS")


__all__ = ["PgChunkRepository"]
