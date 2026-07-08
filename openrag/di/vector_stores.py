"""Factory for the :class:`VectorStore` adapter.

Returns the Phase 7B :class:`services.storage.milvus_store.MilvusVectorStore`
built from ``settings.vectordb``. Construction is I/O-free; the embedder
dependency is materialised later via ``await store.initialize(dim)`` in the
composition root, mirroring the :class:`PostgresStore` lifecycle (cheap
construct, async materialise — see Phase 7B decision #2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.storage.milvus_store import MilvusVectorStore

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.vector_stores import VectorStore


def create_vector_store(settings: Settings) -> VectorStore:
    """Build a :class:`MilvusVectorStore` from the root settings."""
    return MilvusVectorStore(settings.vectordb)


__all__ = ["create_vector_store"]
