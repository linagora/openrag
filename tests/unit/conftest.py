"""Shared unit-test fixtures: dependency-free mock implementations of the
core ports so unit tests can exercise services without real infrastructure.

The mocks subclass the actual port ABCs (``core.embeddings.embedder.Embedder``,
``core.vector_stores.vector_store.VectorStore``) so they stay in sync — adding a
new abstract method makes the mock fail to instantiate until it is implemented.
``CatalogStore`` has too many repository accessors to hand-write, so it is
provided as a ``MagicMock`` spec'd against the real class. ``InMemoryObjectStore``
passes the same conformance suite as the real object stores
(``services/storage/test_in_memory_object_store.py``), so a test can rely on it
behaving like them.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock

import pytest
from core.embeddings.embedder import Embedder
from core.ports.catalog_store import CatalogStore
from core.ports.object_store import ByteRange, ObjectInfo, ObjectStore, validate_metadata
from core.utils.exceptions import ObjectNotFoundError, ObjectStoreError
from core.utils.object_keys import LEGACY, validate_key, validate_suffix
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

    def iter_chunk_metadata(self, collection, *, partition, file_ids=None, batch_size=500):
        raise NotImplementedError("This test double does not model reconciliation metadata")

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.search_results: list[dict[str, Any]] = []
        # Dense fields ensured on this store, with their dimension. A field
        # absent here models "nothing indexed with that embedder yet".
        self.vector_fields: dict[str, int] = {}
        # Partial writes per field: {field: {chunk_id: vector or None}}.
        self.written_vectors: dict[str, dict[str, list[float] | None]] = {}

    async def upsert(
        self,
        chunks: list[Any],
        collection: str = "default",
        *,
        indexed_at=None,
        vector_field=None,
        protected_metadata=None,
    ) -> int:
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
        vector_field: str | None = None,
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

    async def ensure_vector_field(self, field: str, dimension: int) -> bool:
        created = field not in self.vector_fields
        self.vector_fields[field] = dimension
        return created

    async def drop_vector_field(self, field: str) -> bool:
        return self.vector_fields.pop(field, None) is not None

    async def write_vectors(self, field: str, vectors: dict[str, list[float] | None]) -> int:
        self.written_vectors.setdefault(field, {}).update(vectors)
        return len(vectors)

    async def drop_collection(self, name: str) -> None:
        self.collections.pop(name, None)

    async def collection_exists(self, name: str) -> bool:
        return name in self.collections

    async def vector_dimension(self, vector_field: str | None = None) -> int | None:
        if not vector_field:
            return None
        return self.vector_fields.get(vector_field)

    async def query_ids_by_filter(self, collection: str, filters: dict[str, Any]) -> list[str]:
        return list(self.collections.get(collection, {}).keys())

    async def query_chunks_by_filter(
        self,
        collection: str,
        filters: dict[str, Any],
        output_fields: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return []


class InMemoryObjectStore(ObjectStore):
    """An ObjectStore in a dict. ``local_copy`` writes a temporary file, as S3's does."""

    def __init__(self) -> None:
        # key -> (content, sha256, metadata)
        self.objects: dict[str, tuple[bytes, str, dict[str, str]]] = {}

    def _get(self, key: str) -> tuple[bytes, str, dict[str, str]]:
        validate_key(key)
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectNotFoundError(f"No object under {key!r}.") from None

    def _store(self, key: str, data: bytes, digest: str, metadata: dict[str, str]) -> None:
        if key.startswith(f"{LEGACY}/"):
            # As on a filesystem, where a path is a file or a directory.
            for other in self.objects:
                if other.startswith(f"{key}/") or key.startswith(f"{other}/"):
                    raise ObjectStoreError(f"{key!r} and {other!r} can't both hold an object.", key=key)
        self.objects[key] = (data, digest, metadata)

    def _info(self, key: str) -> ObjectInfo:
        data, digest, metadata = self.objects[key]
        return ObjectInfo(key=key, size=len(data), sha256=digest, metadata=MappingProxyType(dict(metadata)))

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], metadata: Mapping[str, str] | None = None
    ) -> ObjectInfo:
        validate_key(key)
        checked = validate_metadata(metadata)
        hasher, parts = hashlib.sha256(), []
        async for chunk in stream:
            hasher.update(chunk)
            parts.append(chunk)
        self._store(key, b"".join(parts), hasher.hexdigest(), checked)
        return self._info(key)

    @asynccontextmanager
    async def open_stream(self, key: str, byte_range: ByteRange | None = None) -> AsyncIterator[AsyncIterator[bytes]]:
        data = self._get(key)[0]
        if byte_range is not None:
            offset, length = byte_range.resolve(len(data))
            data = data[offset : offset + length]

        async def _chunks() -> AsyncIterator[bytes]:
            for start in range(0, len(data), 64 * 1024):
                yield data[start : start + 64 * 1024]

        yield _chunks()

    @asynccontextmanager
    async def local_copy(self, key: str, suffix: str | None = None) -> AsyncIterator[Path]:
        data = self._get(key)[0]
        suffix = PurePosixPath(key).suffix if suffix is None else validate_suffix(suffix)
        fd, name = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        try:
            yield Path(name)
        finally:
            Path(name).unlink(missing_ok=True)

    async def stat(self, key: str) -> ObjectInfo:
        self._get(key)
        return self._info(key)

    async def exists(self, key: str) -> bool:
        validate_key(key)
        return key in self.objects

    async def delete(self, key: str) -> None:
        validate_key(key)
        self.objects.pop(key, None)

    async def delete_many(self, keys: Iterable[str]) -> None:
        keys = [validate_key(key) for key in keys]
        for key in keys:
            self.objects.pop(key, None)

    async def copy(self, src: str, dst: str) -> ObjectInfo:
        validate_key(dst)
        data, digest, metadata = self._get(src)
        self._store(dst, data, digest, dict(metadata))
        return self._info(dst)

    async def presign_get(self, key: str, ttl_seconds: int) -> str | None:
        validate_key(key)
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        return None


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


@pytest.fixture()
def memory_object_store() -> InMemoryObjectStore:
    return InMemoryObjectStore()
