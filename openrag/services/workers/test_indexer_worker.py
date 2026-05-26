from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.models.chunk import Chunk
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from services.workers.indexer_actor import IndexerWorker, _load_document
from services.workers.pipeline_builder import build_indexing_pipeline

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeParser:
    def __init__(self, processed: ProcessedDocument) -> None:
        self.processed = processed
        self.calls: list[Document] = []

    async def parse(self, document: Document) -> ProcessedDocument:
        self.calls.append(document)
        return self.processed

    def supported_types(self) -> list[str]:
        return [DocumentType.TEXT.value]


class FakeChunker:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks

    def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
        return self.chunks


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[1.0] for _ in texts]


class FakeVectorStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.ensure_calls: list[tuple[str, int]] = []

    async def upsert(self, chunks: list[Chunk], collection: str = "default") -> int:
        self.calls.append((chunks, collection))
        return len(chunks)

    async def ensure_collection(self, name: str, dimension: int, **kwargs: Any) -> None:
        self.ensure_calls.append((name, dimension))


def _fake_tsm() -> MagicMock:
    """Task-state-manager mock whose .remote() methods return awaitables."""
    tsm = MagicMock()
    tsm.set_state = MagicMock()
    tsm.set_state.remote = AsyncMock(return_value=None)
    tsm.set_failed_if_not_cancelled = MagicMock()
    tsm.set_failed_if_not_cancelled.remote = AsyncMock(return_value=True)
    return tsm


def _make_pipeline(processed: ProcessedDocument, chunks: list[Chunk]) -> Any:
    return build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker(chunks),
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )


class FakeDocumentRepo:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    async def add_file_to_partition(self, **kwargs: Any) -> bool:
        self.add_calls.append(kwargs)
        return True

    async def update_file_in_partition(self, **kwargs: Any) -> bool:
        self.update_calls.append(kwargs)
        return True


# ---------------------------------------------------------------------------
# Tests — _load_document helper
# ---------------------------------------------------------------------------


def test_load_document_reads_bytes_and_detects_type(tmp_path: Path) -> None:
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.4")
    doc = _load_document(str(p), {"file_id": "fid-1"}, "tenant-a")

    assert doc.raw_bytes == b"%PDF-1.4"
    assert doc.content_type == DocumentType.PDF
    assert doc.partition == "tenant-a"
    assert doc.filename == "fid-1"


def test_load_document_falls_back_to_filename_when_no_file_id(tmp_path: Path) -> None:
    p = tmp_path / "note.txt"
    p.write_bytes(b"hi")
    doc = _load_document(str(p), {}, "p")

    assert doc.filename == "note.txt"


# ---------------------------------------------------------------------------
# Tests — IndexerWorker.process_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_file_success_sets_state_and_returns_count(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")
    processed = ProcessedDocument(document_id="d1", text_blocks=[TextBlock(text="content")])
    chunks = [Chunk(id="c1", text="content", partition="p")]
    pipeline = _make_pipeline(processed, chunks)
    tsm = _fake_tsm()

    worker = IndexerWorker(pipeline=pipeline, task_state_manager=tsm)
    result = await worker.process_file(
        task_id="t1",
        path=str(path),
        metadata={"file_id": "f1"},
        partition="p",
    )

    assert result["stored_count"] == 1
    assert result["stage"] == "stored"
    state_calls = [call.args for call in tsm.set_state.remote.call_args_list]
    assert ("t1", "SERIALIZING") in state_calls
    assert ("t1", "COMPLETED") in state_calls
    tsm.set_failed_if_not_cancelled.remote.assert_not_called()


@pytest.mark.asyncio
async def test_process_file_pipeline_failure_sets_failed_and_reraises(tmp_path: Path) -> None:
    path = tmp_path / "bad.txt"
    path.write_bytes(b"x")

    class BrokenParser:
        async def parse(self, document: Document) -> ProcessedDocument:
            raise RuntimeError("parser exploded")

        def supported_types(self) -> list[str]:
            return [DocumentType.TEXT.value]

    pipeline = build_indexing_pipeline(
        parser=BrokenParser(),
        chunker=FakeChunker([]),
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    tsm = _fake_tsm()
    worker = IndexerWorker(pipeline=pipeline, task_state_manager=tsm)

    with pytest.raises(RuntimeError, match="parser exploded"):
        await worker.process_file(
            task_id="t2",
            path=str(path),
            metadata={},
            partition="p",
        )

    tsm.set_state.remote.assert_called_once_with("t2", "SERIALIZING")
    tsm.set_failed_if_not_cancelled.remote.assert_called_once()
    call_args = tsm.set_failed_if_not_cancelled.remote.call_args
    assert call_args.args[0] == "t2"
    assert "parser exploded" in call_args.args[1]


@pytest.mark.asyncio
async def test_process_file_missing_path_raises_and_sets_failed() -> None:
    processed = ProcessedDocument(document_id="d1", text_blocks=[TextBlock(text="x")])
    pipeline = _make_pipeline(processed, [Chunk(id="c1", text="x")])
    tsm = _fake_tsm()
    worker = IndexerWorker(pipeline=pipeline, task_state_manager=tsm)

    with pytest.raises(FileNotFoundError):
        await worker.process_file(
            task_id="t3",
            path="/nonexistent/file.txt",
            metadata={},
            partition="p",
        )

    tsm.set_failed_if_not_cancelled.remote.assert_called_once()


@pytest.mark.asyncio
async def test_process_file_passes_partition_and_filename_to_row(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello")

    seen_partitions: list[str] = []

    class TrackingChunker:
        def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
            seen_partitions.append(partition)
            return [Chunk(id="c1", text="hello", partition=partition)]

    pipeline = build_indexing_pipeline(
        parser=FakeParser(ProcessedDocument(document_id="d1", text_blocks=[TextBlock(text="hello")])),
        chunker=TrackingChunker(),
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    tsm = _fake_tsm()
    worker = IndexerWorker(pipeline=pipeline, task_state_manager=tsm)
    await worker.process_file(
        task_id="t4",
        path=str(path),
        metadata={"file_id": "fid"},
        partition="tenant-b",
    )

    assert seen_partitions == ["tenant-b"]


@pytest.mark.asyncio
async def test_process_file_creates_catalog_record_after_successful_pipeline(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")
    processed = ProcessedDocument(document_id="d1", text_blocks=[TextBlock(text="content")])
    chunks = [Chunk(id="c1", text="content", partition="p")]
    repo = FakeDocumentRepo()
    worker = IndexerWorker(
        pipeline=_make_pipeline(processed, chunks),
        task_state_manager=_fake_tsm(),
        document_repo=repo,
    )

    await worker.process_file(
        task_id="t-new",
        path=str(path),
        metadata={"file_id": "f1", "relationship_id": "rel", "parent_id": "parent"},
        partition="p",
        user={"id": 42},
    )

    assert repo.add_calls == [
        {
            "file_id": "f1",
            "partition": "p",
            "file_metadata": {"file_id": "f1", "relationship_id": "rel", "parent_id": "parent"},
            "user_id": 42,
            "relationship_id": "rel",
            "parent_id": "parent",
        }
    ]
    assert repo.update_calls == []


@pytest.mark.asyncio
async def test_process_file_updates_catalog_record_on_replace(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")
    processed = ProcessedDocument(document_id="d1", text_blocks=[TextBlock(text="content")])
    chunks = [Chunk(id="c1", text="content", partition="p")]
    repo = FakeDocumentRepo()
    worker = IndexerWorker(
        pipeline=_make_pipeline(processed, chunks),
        task_state_manager=_fake_tsm(),
        document_repo=repo,
    )

    await worker.process_file(
        task_id="t-replace",
        path=str(path),
        metadata={"file_id": "f1"},
        partition="p",
        replace=True,
    )

    assert repo.update_calls == [
        {
            "file_id": "f1",
            "partition": "p",
            "file_metadata": {"file_id": "f1"},
            "relationship_id": None,
            "parent_id": None,
        }
    ]
    assert repo.add_calls == []


@pytest.mark.asyncio
async def test_process_file_catalog_failure_sets_failed_state(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")
    processed = ProcessedDocument(document_id="d1", text_blocks=[TextBlock(text="content")])
    chunks = [Chunk(id="c1", text="content", partition="p")]
    tsm = _fake_tsm()

    class BrokenRepo:
        async def add_file_to_partition(self, **kwargs: Any) -> bool:
            raise RuntimeError("pg down")

    worker = IndexerWorker(
        pipeline=_make_pipeline(processed, chunks),
        task_state_manager=tsm,
        document_repo=BrokenRepo(),
    )

    with pytest.raises(RuntimeError, match="pg down"):
        await worker.process_file(
            task_id="t-fail",
            path=str(path),
            metadata={"file_id": "f1"},
            partition="p",
        )

    tsm.set_failed_if_not_cancelled.remote.assert_called_once()
    completed_calls = [call for call in tsm.set_state.remote.call_args_list if call.args == ("t-fail", "COMPLETED")]
    assert completed_calls == []
