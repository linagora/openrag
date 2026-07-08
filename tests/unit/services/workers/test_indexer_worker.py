from __future__ import annotations

from datetime import datetime
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

    async def upsert(self, chunks: list[Chunk], collection: str = "default", *, indexed_at=None) -> int:
        self.calls.append((chunks, collection, indexed_at))
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


class FakeTopicTagRepo:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.inserted: list[list[dict[str, str]]] = []

    async def delete_by_document(self, document_id: str, partition: str) -> int:
        self.deleted.append((document_id, partition))
        return 0

    async def bulk_insert(self, tags: list[dict]) -> int:
        self.inserted.append(tags)
        return len(tags)


# ---------------------------------------------------------------------------
# Tests — _load_document helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_document_reads_bytes_and_detects_type_from_original_filename(tmp_path: Path) -> None:
    p = tmp_path / "upload-without-extension"
    p.write_bytes(b"%PDF-1.4")
    doc = await _load_document(
        str(p),
        {"file_id": "fid-1", "filename": "safe-name", "original_filename": "report.pdf"},
        "tenant-a",
    )

    assert doc.raw_bytes == b"%PDF-1.4"
    assert doc.content_type == DocumentType.PDF
    assert doc.partition == "tenant-a"
    assert doc.filename == "report.pdf"
    # Document.id must be the file_id (not a random uuid): the chunker derives
    # Chunk.document_id / file_id from ProcessedDocument.document_id == document.id.
    assert doc.id == "fid-1"


@pytest.mark.asyncio
async def test_load_document_requires_file_id(tmp_path: Path) -> None:
    p = tmp_path / "note.txt"
    p.write_bytes(b"hi")

    # file_id is force-set upstream by IndexingService._build_metadata; if it is
    # ever missing we fail loudly rather than persist chunks under a bad id.
    with pytest.raises(ValueError, match="file_id"):
        await _load_document(str(p), {}, "p")


@pytest.mark.asyncio
async def test_load_document_does_not_leak_internal_keys_into_metadata(tmp_path: Path) -> None:
    p = tmp_path / "note.txt"
    p.write_bytes(b"hi")

    doc = await _load_document(str(p), {"file_id": "fid", "source": "note.txt"}, "p")

    # indexation_config reaches the pipeline via row["indexation_config"], never
    # the document metadata, so it cannot leak into chunk metadata.
    assert doc.metadata == {"file_id": "fid", "source": "note.txt"}
    assert all(not key.startswith("_openrag") for key in doc.metadata)


@pytest.mark.asyncio
async def test_load_document_falls_back_to_stored_path_name(tmp_path: Path) -> None:
    p = tmp_path / "audio.flac"
    p.write_bytes(b"flac")

    doc = await _load_document(str(p), {"file_id": "fid"}, "p")

    assert doc.filename == "audio.flac"
    assert doc.content_type == DocumentType.AUDIO


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
async def test_process_file_passes_task_id_to_pipeline_row(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")
    captured: dict[str, Any] = {}

    class RecordingPipeline:
        async def run(self, row: dict[str, Any]) -> dict[str, Any]:
            captured.update(row)
            row["stored_count"] = 1
            row["stage"] = "stored"
            return row

    tsm = _fake_tsm()
    worker = IndexerWorker(pipeline=RecordingPipeline(), task_state_manager=tsm)

    await worker.process_file(
        task_id="t1",
        path=str(path),
        metadata={"file_id": "f1"},
        partition="p",
    )

    assert captured["task_id"] == "t1"


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
            metadata={"file_id": "f1"},
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
            metadata={"file_id": "f1"},
            partition="p",
        )

    tsm.set_failed_if_not_cancelled.remote.assert_called_once()


@pytest.mark.asyncio
async def test_process_file_passes_partition_and_filename_to_row(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello")

    seen_partitions: list[str] = []
    seen_documents: list[Document] = []

    class TrackingChunker:
        def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
            seen_partitions.append(partition)
            return [Chunk(id="c1", text="hello", partition=partition)]

    class TrackingParser:
        async def parse(self, document: Document) -> ProcessedDocument:
            seen_documents.append(document)
            return ProcessedDocument(document_id="d1", text_blocks=[TextBlock(text="hello")])

        def supported_types(self) -> list[str]:
            return [DocumentType.TEXT.value]

    pipeline = build_indexing_pipeline(
        parser=TrackingParser(),
        chunker=TrackingChunker(),
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
    )
    tsm = _fake_tsm()
    worker = IndexerWorker(pipeline=pipeline, task_state_manager=tsm)
    await worker.process_file(
        task_id="t4",
        path=str(path),
        metadata={"file_id": "fid", "original_filename": "original-note.txt"},
        partition="tenant-b",
    )

    assert seen_partitions == ["tenant-b"]
    assert seen_documents[0].filename == "original-note.txt"


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

    assert len(repo.add_calls) == 1
    add_call = repo.add_calls[0]
    assert isinstance(add_call.pop("indexed_at"), datetime)
    assert add_call == {
        "file_id": "f1",
        "partition": "p",
        "file_metadata": {"file_id": "f1", "relationship_id": "rel", "parent_id": "parent"},
        "user_id": 42,
        "relationship_id": "rel",
        "parent_id": "parent",
    }
    assert repo.update_calls == []


@pytest.mark.asyncio
async def test_process_file_shares_one_indexed_at_between_store_and_catalog(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")
    processed = ProcessedDocument(document_id="d1", text_blocks=[TextBlock(text="content")])
    chunks = [Chunk(id="c1", text="content", partition="p")]
    store = FakeVectorStore()
    repo = FakeDocumentRepo()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker(chunks),
        embedder=FakeEmbedder(),
        vector_store=store,
    )
    worker = IndexerWorker(pipeline=pipeline, task_state_manager=_fake_tsm(), document_repo=repo)

    await worker.process_file(task_id="t1", path=str(path), metadata={"file_id": "f1"}, partition="p", user={"id": 1})

    store_indexed_at = store.calls[0][2]
    catalog_indexed_at = repo.add_calls[0]["indexed_at"]
    assert isinstance(store_indexed_at, datetime)
    # The store and the catalog must receive the very same timestamp object/value.
    assert store_indexed_at == catalog_indexed_at


@pytest.mark.asyncio
async def test_process_file_stores_indexation_config_snapshot_on_new_file(tmp_path: Path) -> None:
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
    indexation_config = {"parsing_strategy": "pymupdf", "enable_image_captioning": False}

    await worker.process_file(
        task_id="t-new",
        path=str(path),
        metadata={"file_id": "f1"},
        partition="p",
        user={"id": 42},
        indexation_config=indexation_config,
    )

    assert repo.add_calls[0]["indexation_config"] == indexation_config


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

    assert len(repo.update_calls) == 1
    update_call = repo.update_calls[0]
    assert isinstance(update_call.pop("indexed_at"), datetime)
    assert update_call == {
        "file_id": "f1",
        "partition": "p",
        "file_metadata": {"file_id": "f1"},
        "relationship_id": None,
        "parent_id": None,
    }
    assert repo.add_calls == []


@pytest.mark.asyncio
async def test_process_file_stores_indexation_config_snapshot_on_replace(tmp_path: Path) -> None:
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
    indexation_config = {"parsing_strategy": "marker", "enable_contextualization": True}

    await worker.process_file(
        task_id="t-replace",
        path=str(path),
        metadata={"file_id": "f1"},
        partition="p",
        replace=True,
        indexation_config=indexation_config,
    )

    assert repo.update_calls[0]["indexation_config"] == indexation_config


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


@pytest.mark.asyncio
async def test_process_file_replaces_topic_tags_after_successful_pipeline(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")

    class TaggingPipeline:
        async def run(self, row: dict[str, Any]) -> dict[str, Any]:
            row["topic_tags"] = ["finance", "risk"]
            row["stored_count"] = 1
            row["stage"] = "stored"
            return row

    repo = FakeTopicTagRepo()
    worker = IndexerWorker(
        pipeline=TaggingPipeline(),
        task_state_manager=_fake_tsm(),
        topic_tag_repo=repo,
    )

    await worker.process_file(
        task_id="t-tags",
        path=str(path),
        metadata={"file_id": "f1"},
        partition="tenant-a",
    )

    assert repo.deleted == [("f1", "tenant-a")]
    assert repo.inserted == [
        [
            {"document_id": "f1", "partition": "tenant-a", "tag": "finance"},
            {"document_id": "f1", "partition": "tenant-a", "tag": "risk"},
        ]
    ]


@pytest.mark.asyncio
async def test_process_file_deletes_topic_tags_when_tagging_is_disabled(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")

    class UntaggedPipeline:
        async def run(self, row: dict[str, Any]) -> dict[str, Any]:
            row["stored_count"] = 1
            row["stage"] = "stored"
            return row

    repo = FakeTopicTagRepo()
    worker = IndexerWorker(
        pipeline=UntaggedPipeline(),
        task_state_manager=_fake_tsm(),
        topic_tag_repo=repo,
    )

    await worker.process_file(
        task_id="t-disabled-tags",
        path=str(path),
        metadata={"file_id": "f1"},
        partition="tenant-a",
        indexation_config={"enable_topic_tagging": False},
    )

    assert repo.deleted == [("f1", "tenant-a")]
    assert repo.inserted == []


@pytest.mark.asyncio
async def test_process_file_rejects_malformed_topic_tags_before_delete(tmp_path: Path) -> None:
    path = tmp_path / "doc.txt"
    path.write_bytes(b"content")
    tsm = _fake_tsm()

    class BrokenTaggingPipeline:
        async def run(self, row: dict[str, Any]) -> dict[str, Any]:
            row["topic_tags"] = "finance"
            row["stored_count"] = 1
            row["stage"] = "stored"
            return row

    repo = FakeTopicTagRepo()
    worker = IndexerWorker(
        pipeline=BrokenTaggingPipeline(),
        task_state_manager=tsm,
        topic_tag_repo=repo,
    )

    with pytest.raises(TypeError, match="topic_tags"):
        await worker.process_file(
            task_id="t-bad-tags",
            path=str(path),
            metadata={"file_id": "f1"},
            partition="tenant-a",
        )

    assert repo.deleted == []
    assert repo.inserted == []
    tsm.set_failed_if_not_cancelled.remote.assert_called_once()
