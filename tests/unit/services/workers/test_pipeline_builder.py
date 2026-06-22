import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.models.chunk import Chunk
from core.models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
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

    async def upsert(self, chunks: list[Chunk], collection: str = "default", *, indexed_at=None) -> int:
        self.calls.append((chunks, collection))
        return len(chunks)

    async def ensure_collection(self, name: str, dimension: int, **kwargs) -> None:
        self.ensure_calls.append((name, dimension))


class FakeVLM:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        self.calls.append(image_bytes)
        return "caption"


class FakeContextualizer:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Chunk], str, str]] = []

    async def contextualize(self, chunks, *, filename: str = "", lang: str = "en") -> list[Chunk]:
        self.calls.append((list(chunks), filename, lang))
        return [chunk.model_copy(update={"text": f"ctx {chunk.text}", "context": "ctx"}) for chunk in chunks]


class FakeTopicTagger:
    def __init__(self, tags: list[str] | None = None) -> None:
        self.tags = tags or ["finance", "risk"]
        self.calls: list[tuple[list[Chunk], str, int, str]] = []

    async def tag(
        self,
        chunks,
        *,
        filename: str = "",
        max_tags: int = 7,
        lang: str = "en",
    ) -> list[str]:
        self.calls.append((list(chunks), filename, max_tags, lang))
        return self.tags


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


@pytest.mark.asyncio
async def test_pipeline_indexation_config_disables_caption_and_contextualization():
    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[TextBlock(text="hello")],
        images=[ImageBlock(image_bytes=b"png")],
    )
    chunks = [Chunk(id="c1", text="hello", partition="tenant-a")]
    vlm = FakeVLM()
    contextualizer = FakeContextualizer()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker(chunks),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        vlm=vlm,
        contextualizer=contextualizer,
        indexation_config=IndexationPipelineConfig(
            enable_image_captioning=False,
            enable_contextualization=False,
        ),
    )

    row = {"document": document, "partition": "tenant-a", "filename": "note.txt"}
    await pipeline.run(row)

    assert vlm.calls == []
    assert contextualizer.calls == []
    assert row["stage"] == "stored"


@pytest.mark.asyncio
async def test_pipeline_row_indexation_config_selects_components():
    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    default_processed = ProcessedDocument(document_id="default", text_blocks=[TextBlock(text="default")])
    selected_processed = ProcessedDocument(
        document_id="selected",
        text_blocks=[TextBlock(text="selected")],
        images=[ImageBlock(image_bytes=b"png")],
    )
    selected_chunk = Chunk(id="selected", text="selected", partition="tenant-a")
    selected_parser = FakeParser(selected_processed)
    selected_chunker = FakeChunker([selected_chunk])
    selected_embedder = FakeEmbedder([[0.5]])
    selected_vlm = FakeVLM()
    selected_contextualizer = FakeContextualizer()
    selected_topic_tagger = FakeTopicTagger(["portfolio"])
    parser_calls: list[str] = []
    chunker_calls: list[object] = []
    embedder_calls: list[str] = []
    vlm_calls: list[str] = []
    contextualizer_calls: list[str] = []
    topic_tagger_calls: list[str] = []

    pipeline = build_indexing_pipeline(
        parser=FakeParser(default_processed),
        chunker=FakeChunker([Chunk(id="default", text="default")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        parser_factory=lambda name: parser_calls.append(name) or selected_parser,
        chunker_factory=lambda config: chunker_calls.append(config) or selected_chunker,
        embedder_factory=lambda name: embedder_calls.append(name) or selected_embedder,
        vlm_factory=lambda name: vlm_calls.append(name) or selected_vlm,
        contextualizer_factory=lambda name: contextualizer_calls.append(name) or selected_contextualizer,
        topic_tagger_factory=lambda name: topic_tagger_calls.append(name) or selected_topic_tagger,
    )
    row = {
        "document": document,
        "partition": "tenant-a",
        "filename": "note.txt",
        "embedder_name": "embed-fast",
        "indexation_config": {
            "parsing_strategy": "pymupdf",
            "chunking": {"name": "recursive_splitter", "chunk_size": 128, "chunk_overlap_rate": 0.1},
            "enable_image_captioning": True,
            "vlm": "vlm-fast",
            "enable_contextualization": True,
            "contextualization_llm": "llm-context",
            "enable_topic_tagging": True,
            "topic_tagging_llm": "llm-topic",
            "max_topic_tags": 3,
        },
    }

    await pipeline.run(row)

    assert parser_calls == ["pymupdf"]
    assert chunker_calls[0].chunk_size == 128
    assert embedder_calls == ["embed-fast"]
    assert vlm_calls == ["vlm-fast"]
    assert contextualizer_calls == ["llm-context"]
    assert topic_tagger_calls == ["llm-topic"]
    assert selected_parser.calls == [document]
    assert selected_chunker.calls == [(row["processed_document"], "tenant-a")]
    assert selected_vlm.calls == [b"png"]
    assert selected_contextualizer.calls[0][1:] == ("note.txt", "en")
    assert selected_topic_tagger.calls == [
        ([row["chunks"][0].model_copy(update={"embedding": None})], "note.txt", 3, "en")
    ]
    assert row["topic_tags"] == ["portfolio"]
    assert row["chunks"][0].embedding == [0.5]


@pytest.mark.asyncio
async def test_pipeline_indexation_config_disables_topic_tagging():
    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(document_id=document.id, text_blocks=[TextBlock(text="hello")])
    chunks = [Chunk(id="c1", text="hello", partition="tenant-a")]
    topic_tagger = FakeTopicTagger()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker(chunks),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        topic_tagger=topic_tagger,
        indexation_config=IndexationPipelineConfig(enable_topic_tagging=False),
    )

    row = {"document": document, "partition": "tenant-a", "filename": "note.txt"}
    await pipeline.run(row)

    assert topic_tagger.calls == []
    assert row.get("topic_tags") is None
