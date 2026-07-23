"""Shared unit-test fixtures: dependency-free mock implementations of the
core ports so unit tests can exercise services without real infrastructure.

The mocks subclass the actual port ABCs (``core.embeddings.embedder.Embedder``,
``core.vector_stores.vector_store.VectorStore``) so they stay in sync — adding a
new abstract method makes the mock fail to instantiate until it is implemented.
``CatalogStore`` has too many repository accessors to hand-write, so it is
provided as a ``MagicMock`` spec'd against the real class.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from core.embeddings.embedder import Embedder
from core.ports.catalog_store import CatalogStore
from core.vector_stores.vector_store import VectorStore


class MockEmbedder(Embedder):
    """Deterministic in-memory embedder. Returns fixed-dimension vectors."""

    def __init__(self, dimension: int = 8) -> None:
        self._dimension = dimension
        self.embed_calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return [self._vector(t) for t in texts]

    async def embed_single(self, text: str) -> list[float]:
        return self._vector(text)

    @property
    def dimension(self) -> int:
        return self._dimension

    def _vector(self, text: str) -> list[float]:
        # Stable pseudo-embedding derived from the text length so equal inputs
        # yield equal vectors without any external service.
        seed = float(len(text) % 7)
        return [seed + i for i in range(self._dimension)]


class MockVectorStore(VectorStore):
    """In-memory vector store keyed by collection name."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.search_results: list[dict[str, Any]] = []

    async def upsert(self, chunks: list[Any], collection: str = "default", *, indexed_at=None) -> int:
        store = self.collections.setdefault(collection, {})
        for chunk in chunks:
            store[getattr(chunk, "id", id(chunk))] = chunk
        return len(chunks)

    async def search(
        self,
        embedding: list[float],
        query_text: str | None = None,
        top_k: int = 10,
        collection: str = "default",
        filters: dict[str, Any] | None = None,
        similarity_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        return self.search_results[:top_k]

    async def delete(self, ids: list[str], collection: str = "default") -> int:
        store = self.collections.get(collection, {})
        removed = 0
        for cid in ids:
            if store.pop(cid, None) is not None:
                removed += 1
        return removed

    async def delete_by_filter(self, filters: dict[str, Any]) -> int:
        removed = 0
        for collection in self.collections.values():
            for cid, chunk in list(collection.items()):
                if all(getattr(chunk, key, None) == value for key, value in filters.items()):
                    collection.pop(cid, None)
                    removed += 1
        return removed

    async def ensure_collection(self, name: str, dimension: int, **kwargs: Any) -> None:
        self.collections.setdefault(name, {})

    async def drop_collection(self, name: str) -> None:
        self.collections.pop(name, None)

    async def collection_exists(self, name: str) -> bool:
        return name in self.collections

    async def query_ids_by_filter(self, collection: str, filters: dict[str, Any]) -> list[str]:
        return list(self.collections.get(collection, {}).keys())

    async def query_chunks_by_filter(
        self,
        collection: str,
        filters: dict[str, Any],
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return []


@pytest.fixture()
def mock_embedder() -> MockEmbedder:
    return MockEmbedder()


@pytest.fixture()
def mock_vector_store() -> MockVectorStore:
    return MockVectorStore()


@pytest.fixture()
def mock_catalog_store() -> MagicMock:
    """A ``CatalogStore`` whose repository accessors return autospec'd mocks."""
    return MagicMock(spec=CatalogStore)
