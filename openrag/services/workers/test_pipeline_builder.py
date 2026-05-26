import pytest
from core.models.chunk import Chunk
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from services.workers.pipeline_builder import build_indexing_pipeline


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
    def __init__(self, chunks: list[Chunk], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.calls: list[tuple[ProcessedDocument, str]] = []

    def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
        self.calls.append((document, partition))
        if self.error is not None:
            raise self.error
        return self.chunks


class FakeEmbedder:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


class FakeVectorStore:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Chunk], str]] = []
        self.ensure_calls: list[tuple[str, int]] = []

    async def upsert(self, chunks: list[Chunk], collection: str = "default") -> int:
        self.calls.append((chunks, collection))
        return len(chunks)

    async def ensure_collection(self, name: str, dimension: int, **kwargs) -> None:
        self.ensure_calls.append((name, dimension))


@pytest.mark.asyncio
async def test_pipeline_runs_required_stages_in_order_and_keeps_row_object():
    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(document_id=document.id, text_blocks=[TextBlock(text="hello")])
    chunks = [Chunk(id="c1", text="hello", partition="tenant-a")]
    parser = FakeParser(processed)
    chunker = FakeChunker(chunks)
    embedder = FakeEmbedder([[1.0, 0.0]])
    vector_store = FakeVectorStore()
    pipeline = build_indexing_pipeline(
        parser=parser,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
    )
    row = {"document": document, "partition": "tenant-a", "token": "secret"}

    result = await pipeline.run(row)

    assert result is row
    assert parser.calls == [document]
    assert chunker.calls == [(processed, "tenant-a")]
    assert embedder.calls == [["hello"]]
    assert vector_store.ensure_calls == [("default", 2)]
    assert vector_store.calls == [(row["chunks"], "default")]
    assert row["stage"] == "stored"
    assert row["stored_count"] == 1
    assert row["chunks"][0].embedding == [1.0, 0.0]
    assert "token" not in row


@pytest.mark.asyncio
async def test_pipeline_stops_before_later_stages_when_a_stage_fails():
    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(document_id=document.id, text_blocks=[TextBlock(text="hello")])
    chunker = FakeChunker([], error=RuntimeError("chunk failed"))
    vector_store = FakeVectorStore()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=chunker,
        embedder=FakeEmbedder([]),
        vector_store=vector_store,
    )
    row = {"document": document, "password": "secret"}

    with pytest.raises(RuntimeError, match="chunk failed"):
        await pipeline.run(row)

    assert row["stage"] == "chunk_failed"
    assert row["error"] == "chunk failed"
    assert vector_store.calls == []
    assert "password" not in row
