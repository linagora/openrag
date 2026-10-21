"""Integration proof for #657 — re-indexing must not duplicate Milvus chunks.

Drives the real :class:`IndexingPipeline` (with trivial parser/chunker/embedder
fakes) against a live Milvus 3.0, indexing a file and then re-indexing it with
``replace=True``. Asserts the file's chunk count stays stable (insert-before-
delete) instead of doubling on every re-index.

Auto-skips when Milvus isn't reachable. Run against the integration stack:

    docker compose -f tests/integration/repos/docker-compose.yaml up -d
    uv run pytest tests/integration/test_reindex_no_duplicate_integration.py -m integration
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator

import pytest
from core.config.infrastructure import VectorDBConfig
from core.models.chunk import Chunk, ChunkType
from core.models.document import Document, ProcessedDocument, TextBlock
from services.storage.milvus_store import MilvusVectorStore
from services.workers.pipeline_builder import build_indexing_pipeline

pytestmark = pytest.mark.integration


def _milvus_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture
def store() -> Iterator[MilvusVectorStore]:
    host = os.getenv("OPENRAG_TEST_VDB_HOST", "localhost")
    port = int(os.getenv("OPENRAG_TEST_VDB_PORT", "19530"))
    if not _milvus_reachable(host, port):
        pytest.skip(f"Milvus not reachable at {host}:{port}")
    config = VectorDBConfig(
        host=host,
        port=port,
        collection_name=f"itest_{uuid.uuid4().hex[:12]}",
        hybrid_search=True,
        schema_version=1,
    )
    s = MilvusVectorStore(config)
    try:
        yield s
    finally:
        try:
            if s._client.has_collection(config.collection_name):
                s._client.drop_collection(config.collection_name)
        except Exception:
            pass


class _FakeParser:
    def __init__(self, n_blocks: int) -> None:
        self.n_blocks = n_blocks

    async def parse(self, document: Document) -> ProcessedDocument:
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=[TextBlock(text=f"{document.id} block {i}") for i in range(self.n_blocks)],
        )

    def supported_types(self) -> list[str]:
        return ["text"]


class _FakeChunker:
    """One chunk per text block, carrying the file identity Milvus filters on."""

    def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
        return [
            Chunk(
                id=f"{document.document_id}-{i}",
                text=block.text,
                partition=partition,
                document_id=document.document_id,
                chunk_type=ChunkType.TEXT,
            )
            for i, block in enumerate(document.text_blocks)
        ]


class _FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


async def _count_chunks(store: MilvusVectorStore, partition: str, file_id: str) -> int:
    ids = await store.query_ids_by_filter("default", {"partition": partition, "file_id": file_id})
    return len(ids)


@pytest.mark.asyncio
async def test_reindex_replaces_chunks_instead_of_duplicating(store: MilvusVectorStore) -> None:
    partition = "tenant-a"
    file_id = "reindex-me"
    pipeline = build_indexing_pipeline(
        parser=_FakeParser(n_blocks=3),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vector_store=store,
    )

    def _row(replace: bool) -> dict:
        return {
            "document": Document(id=file_id, filename="doc.txt", text="x", partition=partition),
            "partition": partition,
            "replace": replace,
            "task_id": "t-657",
        }

    # First index: 3 chunks.
    await pipeline.run(_row(replace=False))
    first_ids = set(await store.query_ids_by_filter("default", {"partition": partition, "file_id": file_id}))
    assert len(first_ids) == 3

    # Re-index twice with replace=True: count must stay at 3, not grow to 6 then 9.
    await pipeline.run(_row(replace=True))
    assert await _count_chunks(store, partition, file_id) == 3

    await pipeline.run(_row(replace=True))
    second_ids = set(await store.query_ids_by_filter("default", {"partition": partition, "file_id": file_id}))
    assert len(second_ids) == 3

    # The surviving chunks are the freshly-stored set — the originals were deleted.
    assert first_ids.isdisjoint(second_ids)


@pytest.mark.asyncio
async def test_reindex_does_not_touch_other_files(store: MilvusVectorStore) -> None:
    partition = "tenant-a"
    pipeline = build_indexing_pipeline(
        parser=_FakeParser(n_blocks=2),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vector_store=store,
    )

    def _row(file_id: str, replace: bool) -> dict:
        return {
            "document": Document(id=file_id, filename=f"{file_id}.txt", text="x", partition=partition),
            "partition": partition,
            "replace": replace,
        }

    await pipeline.run(_row("file-a", replace=False))
    await pipeline.run(_row("file-b", replace=False))
    # Re-index file-a only; file-b must be untouched.
    await pipeline.run(_row("file-a", replace=True))

    assert await _count_chunks(store, partition, "file-a") == 2
    assert await _count_chunks(store, partition, "file-b") == 2


@pytest.mark.asyncio
async def test_reindex_is_scoped_by_partition(store: MilvusVectorStore) -> None:
    # Cleanup is filtered by (partition, file_id): the SAME file_id in a different
    # partition must be untouched when one partition's copy is re-indexed.
    file_id = "shared-id"
    pipeline = build_indexing_pipeline(
        parser=_FakeParser(n_blocks=2),
        chunker=_FakeChunker(),
        embedder=_FakeEmbedder(),
        vector_store=store,
    )

    def _row(partition: str, replace: bool) -> dict:
        return {
            "document": Document(id=file_id, filename="doc.txt", text="x", partition=partition),
            "partition": partition,
            "replace": replace,
        }

    await pipeline.run(_row("tenant-a", replace=False))
    await pipeline.run(_row("tenant-b", replace=False))
    tenant_b_ids = set(await store.query_ids_by_filter("default", {"partition": "tenant-b", "file_id": file_id}))
    assert len(tenant_b_ids) == 2

    # Re-index the file in tenant-a only.
    await pipeline.run(_row("tenant-a", replace=True))

    assert await _count_chunks(store, "tenant-a", file_id) == 2
    # tenant-b's chunks are the exact same rows as before — untouched.
    assert (
        set(await store.query_ids_by_filter("default", {"partition": "tenant-b", "file_id": file_id})) == tenant_b_ids
    )
