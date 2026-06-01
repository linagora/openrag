"""Unit tests for :class:`ConversionService` (Phase 8E)."""

from __future__ import annotations

import pytest
from services.orchestrators.conversion_service import ConversionService


class FakeSerializer:
    def __init__(self, *, content="  raw\x00 text  "):
        self._content = content
        self.calls: list[tuple[str, dict]] = []

    async def serialize(self, path: str, metadata: dict) -> str:
        self.calls.append((path, metadata))
        return self._content


class FakeVectorStore:
    def __init__(self, *, rows=None):
        self._rows = rows if rows is not None else []
        self.queries: list[tuple[str, dict]] = []

    async def query_chunks_by_filter(self, collection, filters, output_fields=None):
        self.queries.append((collection, filters))
        return list(self._rows)


def _service(*, serializer=None, store=None):
    return ConversionService(
        serializer=serializer or FakeSerializer(),
        vector_store=store or FakeVectorStore(),
        collection="chunks",
    )


@pytest.mark.asyncio
async def test_serialize_file_merges_metadata_and_sanitizes():
    ser = FakeSerializer(content="hello\x00world")
    svc = _service(serializer=ser)

    out = await svc.serialize_file(
        file_path="/tmp/x.pdf",
        filename="x.pdf",
        metadata={"author": "a"},
    )

    # null byte stripped by sanitize_extracted_text
    assert "\x00" not in out
    assert "hello" in out and "world" in out
    path, md = ser.calls[0]
    assert path == "/tmp/x.pdf"
    assert md == {"author": "a", "source": "/tmp/x.pdf", "filename": "x.pdf"}


@pytest.mark.asyncio
async def test_get_chunk_returns_page_content_and_metadata():
    store = FakeVectorStore(rows=[{"text": "chunk body", "vector": [0.1], "partition": "p1", "file_id": "f1"}])
    svc = _service(store=store)

    chunk = await svc.get_chunk("42")

    assert chunk == {
        "page_content": "chunk body",
        "metadata": {"partition": "p1", "file_id": "f1"},
    }
    # queried Milvus _id as int
    assert store.queries == [("chunks", {"_id": 42})]


@pytest.mark.asyncio
async def test_get_chunk_invalid_id_returns_none_without_query():
    store = FakeVectorStore(rows=[{"text": "x"}])
    svc = _service(store=store)

    assert await svc.get_chunk("not-an-int") is None
    assert store.queries == []


@pytest.mark.asyncio
async def test_get_chunk_missing_returns_none():
    svc = _service(store=FakeVectorStore(rows=[]))
    assert await svc.get_chunk("7") is None
