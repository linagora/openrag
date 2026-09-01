from contextlib import asynccontextmanager

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.indexing.structure_normalizer import DocumentStructureNormalizer
from core.models.chunk import Chunk
from core.models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from services.workers.pipeline_builder import build_indexing_pipeline
from services.workers.stages import caption as caption_module


@pytest.fixture(autouse=True)
def _noop_vlm_semaphore(monkeypatch):
    """Stub the cluster-wide VLM semaphore so caption tests don't boot Ray."""

    @asynccontextmanager
    async def _noop():
        yield

    monkeypatch.setattr(caption_module, "get_vlm_semaphore", _noop)


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
        self.prompts: list[str | None] = []

    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        self.calls.append(image_bytes)
        self.prompts.append(prompt)
        return "caption"


class FakeContextualizer:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Chunk], str, str]] = []

    async def contextualize(
        self, chunks, *, filename: str = "", lang: str = "en", system_prompt: str | None = None
    ) -> list[Chunk]:
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
        system_prompt: str | None = None,
    ) -> list[str]:
        self.calls.append((list(chunks), filename, max_tags, lang))
        return self.tags


class FakeStructureNormalizer(DocumentStructureNormalizer):
    def __init__(self) -> None:
        self.calls = []

    async def normalize(self, document, processed_document, config):
        self.calls.append((document, processed_document, config))
        return processed_document.model_copy(
            update={"normalized_text_blocks": [TextBlock(text="normalized row", page_number=1)]}
        )


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
async def test_pipeline_normalizes_automatic_pdf_after_preserving_raw_blocks():
    document = Document(
        filename="table.pdf",
        content_type=DocumentType.PDF,
        raw_bytes=b"pdf",
        partition="tenant-a",
    )
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[TextBlock(text="parser row", page_number=1)],
        page_count=1,
    )
    normalizer = FakeStructureNormalizer()
    chunker = FakeChunker([Chunk(id="c1", text="normalized row", partition="tenant-a")])
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=chunker,
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        structure_normalizer=normalizer,
        indexation_config=IndexationPipelineConfig(
            table_reconstruction={"mode": "automatic"},
            enable_image_captioning=False,
        ),
    )

    await pipeline.run({"document": document, "partition": "tenant-a"})

    assert len(normalizer.calls) == 1
    normalized_input = normalizer.calls[0][1]
    assert normalized_input.raw_text_blocks[0].text == "parser row"
    assert chunker.calls[0][0].effective_text_blocks()[0].text == "normalized row"


@pytest.mark.asyncio
async def test_pipeline_does_not_allocate_or_normalize_when_feature_is_disabled():
    document = Document(filename="table.pdf", content_type=DocumentType.PDF, raw_bytes=b"pdf")
    processed = ProcessedDocument(text_blocks=[TextBlock(text="parser row", page_number=1)])
    normalizer = FakeStructureNormalizer()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([Chunk(id="c1", text="parser row")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        structure_normalizer=normalizer,
        indexation_config=IndexationPipelineConfig(enable_image_captioning=False),
    )

    await pipeline.run({"document": document})

    assert normalizer.calls == []
    assert pipeline.parser.processed.raw_text_blocks is None


def _caption_pipeline(vlm: FakeVLM, caption_prompt: str | None):
    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[TextBlock(text="hello")],
        images=[ImageBlock(image_bytes=b"png")],
    )
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([Chunk(id="c1", text="hello", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        vlm=vlm,
        caption_prompt=caption_prompt,
    )
    return pipeline, document


@pytest.mark.asyncio
async def test_pipeline_injects_configured_caption_prompt():
    # The captioning template configured on the pipeline (image_describer) must
    # reach the VLM instead of its bare built-in fallback.
    vlm = FakeVLM()
    pipeline, document = _caption_pipeline(vlm, caption_prompt="DESCRIBE THE IMAGE TEMPLATE")
    await pipeline.run({"document": document, "partition": "tenant-a", "filename": "note.txt"})
    assert vlm.prompts == ["DESCRIBE THE IMAGE TEMPLATE"]


@pytest.mark.asyncio
async def test_pipeline_row_caption_prompt_overrides_configured_default():
    # An explicit per-row caption_prompt wins over the pipeline default.
    vlm = FakeVLM()
    pipeline, document = _caption_pipeline(vlm, caption_prompt="DEFAULT TEMPLATE")
    await pipeline.run(
        {"document": document, "partition": "tenant-a", "filename": "note.txt", "caption_prompt": "ROW OVERRIDE"}
    )
    assert vlm.prompts == ["ROW OVERRIDE"]


@pytest.mark.asyncio
async def test_pipeline_falls_back_to_existing_vlm_when_default_registry_vlm_is_missing():
    def _missing_default(_name: str):
        raise KeyError("Unknown vlm 'default'")

    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[TextBlock(text="hello")],
        images=[ImageBlock(image_bytes=b"png")],
    )
    chunks = [Chunk(id="c1", text="hello", partition="tenant-a")]
    vlm = FakeVLM()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker(chunks),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        vlm=vlm,
        vlm_factory=_missing_default,
    )
    row = {
        "document": document,
        "partition": "tenant-a",
        "filename": "note.txt",
        "indexation_config": {
            "enable_image_captioning": True,
        },
    }

    await pipeline.run(row)

    assert vlm.calls == [b"png"]


@pytest.mark.asyncio
async def test_pipeline_falls_back_when_explicit_named_vlm_is_missing():
    # A named VLM endpoint deleted/renamed after assignment must not fail the
    # whole indexing job — captioning is enrichment, so it falls back to the
    # legacy VLM with a warning (mirrors the contextualizer/topic-tagger paths).
    def _missing_named(name: str):
        raise KeyError(f"Unknown vlm '{name}'")

    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[TextBlock(text="hello")],
        images=[ImageBlock(image_bytes=b"png")],
    )
    fallback_vlm = FakeVLM()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([Chunk(id="c1", text="hello", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        vlm=fallback_vlm,
        vlm_factory=_missing_named,
    )
    row = {
        "document": document,
        "partition": "tenant-a",
        "filename": "note.txt",
        "indexation_config": {
            "enable_image_captioning": True,
            "vlm": "missing-vlm",
        },
    }

    await pipeline.run(row)  # no raise

    assert fallback_vlm.calls == [b"png"]  # captioned via the legacy fallback VLM


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
async def test_pipeline_uses_named_embedder_and_vlm_hydrated_after_factory_build():
    from core.config.model_endpoints import ModelEndpointConfig
    from core.embeddings import embedder_registry
    from core.vlm import vlm_registry
    from services.workers.indexer_pool import _build_embedder_factory, _build_vlm_factory

    class ProbeEmbedder:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls: list[list[str]] = []
            self.instances.append(self)

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            return [[0.25] for _ in texts]

    class ProbeVLM:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.calls: list[bytes] = []
            self.instances.append(self)

        async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
            self.calls.append(image_bytes)
            return "named caption"

    embedder_registry.register("e2e-probe-embedder")(ProbeEmbedder)
    vlm_registry.register("e2e-probe-vlm")(ProbeVLM)
    try:
        embedder_endpoints: dict = {}
        vlm_endpoints: dict = {}
        cfg = type(
            "Cfg",
            (),
            {
                "models": type("Models", (), {"embedder": embedder_endpoints, "vlm": vlm_endpoints})(),
                "embedder": type("EmbedderCfg", (), {"base_url": "", "model_name": "", "api_key": ""})(),
                "vlm": type(
                    "VLMCfg",
                    (),
                    {"base_url": "", "model": "", "api_key": "", "timeout": 60, "enable_thinking": None},
                )(),
            },
        )()

        embedder_factory = _build_embedder_factory(cfg)
        vlm_factory = _build_vlm_factory(cfg)

        embedder_endpoints["named-embedder"] = ModelEndpointConfig(
            endpoint="http://embedder.example/v1",
            model_name="embed-model",
            extra={"implementation": "e2e-probe-embedder"},
        )
        vlm_endpoints["named-vlm"] = ModelEndpointConfig(
            endpoint="http://vlm.example/v1",
            model_name="vlm-model",
            extra={"implementation": "e2e-probe-vlm"},
        )

        document = Document(filename="note.txt", text="hello", partition="tenant-a")
        processed = ProcessedDocument(
            document_id=document.id,
            text_blocks=[TextBlock(text="hello")],
            images=[ImageBlock(image_bytes=b"png")],
        )
        default_embedder = FakeEmbedder([[9.9]])
        default_vlm = FakeVLM()
        pipeline = build_indexing_pipeline(
            parser=FakeParser(processed),
            chunker=FakeChunker([Chunk(id="c1", text="hello", partition="tenant-a")]),
            embedder=default_embedder,
            vector_store=FakeVectorStore(),
            vlm=default_vlm,
            embedder_factory=embedder_factory,
            vlm_factory=vlm_factory,
        )
        row = {
            "document": document,
            "partition": "tenant-a",
            "filename": "note.txt",
            "embedder_name": "named-embedder",
            "indexation_config": {
                "enable_image_captioning": True,
                "vlm": "named-vlm",
                "enable_topic_tagging": False,
            },
        }

        await pipeline.run(row)

        assert default_embedder.calls == []
        assert default_vlm.calls == []
        assert ProbeEmbedder.instances[0].calls == [["hello"]]
        assert ProbeVLM.instances[0].calls == [b"png"]
        assert ProbeEmbedder.instances[0].kwargs["endpoint"] == "http://embedder.example/v1"
        assert ProbeVLM.instances[0].kwargs["endpoint"] == "http://vlm.example/v1"
        assert row["chunks"][0].embedding == [0.25]
    finally:
        embedder_registry._registry.pop("e2e-probe-embedder", None)
        vlm_registry._registry.pop("e2e-probe-vlm", None)


@pytest.mark.asyncio
async def test_pipeline_skips_contextualization_when_llm_unresolvable():
    # An enabled contextualization LLM the factory can't resolve must NOT fail the
    # whole file — the stage is skipped and indexing completes.
    def _raise(name: str):
        raise KeyError(f"Unknown llm '{name}'. Available: []")

    pipeline = build_indexing_pipeline(
        parser=FakeParser(ProcessedDocument(document_id="d", text_blocks=[TextBlock(text="t")])),
        chunker=FakeChunker([Chunk(id="c", text="t", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        contextualizer_factory=_raise,
    )
    row = {
        "document": Document(filename="note.txt", text="hello", partition="tenant-a"),
        "partition": "tenant-a",
        "filename": "note.txt",
        "indexation_config": {
            "enable_contextualization": True,
            "contextualization_llm": "missing-endpoint",
        },
    }
    result = await pipeline.run(row)  # must not raise
    assert result is row


@pytest.mark.asyncio
async def test_pipeline_propagates_non_keyerror_contextualizer_factory_error():
    # Only an unresolvable-endpoint KeyError is skipped. A different factory
    # error (e.g. a broken prompt/client config) is a real fault and must
    # surface, not be silently swallowed (the file should fail loudly).
    def _raise(name: str):
        raise ValueError("prompt template failed to load")

    pipeline = build_indexing_pipeline(
        parser=FakeParser(ProcessedDocument(document_id="d", text_blocks=[TextBlock(text="t")])),
        chunker=FakeChunker([Chunk(id="c", text="t", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        contextualizer_factory=_raise,
    )
    row = {
        "document": Document(filename="note.txt", text="hello", partition="tenant-a"),
        "partition": "tenant-a",
        "filename": "note.txt",
        "indexation_config": {
            "enable_contextualization": True,
            "contextualization_llm": "ctx",
        },
    }
    with pytest.raises(ValueError, match="prompt template"):
        await pipeline.run(row)


@pytest.mark.asyncio
async def test_pipeline_always_captions_standalone_image_even_when_partition_disables_it():
    # A standalone image file's caption is its only text content, so it is
    # captioned even when the partition's enable_image_captioning is off
    # (legacy parity).
    document = Document(filename="logo.png", content_type=DocumentType.IMAGE, raw_bytes=b"img")
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[],
        images=[ImageBlock(image_bytes=b"png")],
    )
    vlm = FakeVLM()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([Chunk(id="c1", text="caption", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        vlm=vlm,
        indexation_config=IndexationPipelineConfig(enable_image_captioning=False),
    )
    row = {"document": document, "partition": "tenant-a", "filename": "logo.png"}

    await pipeline.run(row)

    assert vlm.calls == [b"png"]


@pytest.mark.asyncio
async def test_pipeline_skips_embedded_image_caption_when_partition_disables_it():
    # Images embedded in a non-image document stay gated by the per-partition
    # (preset) enable_image_captioning setting.
    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[TextBlock(text="hello")],
        images=[ImageBlock(image_bytes=b"png")],
    )
    vlm = FakeVLM()
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([Chunk(id="c1", text="hello", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        vlm=vlm,
        indexation_config=IndexationPipelineConfig(enable_image_captioning=False),
    )
    row = {"document": document, "partition": "tenant-a", "filename": "note.txt"}

    await pipeline.run(row)

    assert vlm.calls == []


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


@pytest.mark.asyncio
async def test_pipeline_logs_per_stage_timings(monkeypatch):
    """run() emits one structured line per file with per-stage durations so a
    slow backend/stage (e.g. docling vs marker parse) is visible in the logs."""
    import services.workers.pipeline_builder as pb

    class _RecordingLogger:
        def __init__(self) -> None:
            self.bound: dict | None = None
            self.message: str | None = None
            self.debug_bound: dict | None = None
            self.debug_message: str | None = None

        def bind(self, **kwargs):
            self.bound = kwargs
            return self

        def info(self, message: str) -> None:
            self.message = message

        def debug(self, message: str) -> None:
            self.debug_message = message
            self.debug_bound = self.bound

    recorder = _RecordingLogger()
    monkeypatch.setattr(pb, "logger", recorder)

    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(document_id=document.id, text_blocks=[TextBlock(text="hello")])
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([Chunk(id="c1", text="hello", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0, 0.0]]),
        vector_store=FakeVectorStore(),
    )

    await pipeline.run({"task_id": "t1", "document": document, "partition": "tenant-a", "filename": "note.txt"})

    assert recorder.message == "indexing stage timings (ms)"
    assert recorder.bound is not None
    for key in ("ms_parse", "ms_chunk", "ms_embed", "ms_store", "ms_total"):
        assert key in recorder.bound
    # Optional stages weren't configured, so they aren't timed.
    assert "ms_caption" not in recorder.bound
    assert recorder.bound["task_id"] == "t1"
    assert recorder.bound["n_chunks"] == 1
    assert recorder.bound["filename"] == "note.txt"

    # The endpoint-resolution breadcrumb fired too: no VLM/contextualizer/topic
    # tagger were configured, and the row carried no embedder_name.
    assert recorder.debug_message == "model endpoints resolved for indexing (None = stage disabled)"
    assert recorder.debug_bound is not None
    assert recorder.debug_bound["vlm"] is None
    assert recorder.debug_bound["contextualization_llm"] is None
    assert recorder.debug_bound["topic_tagging_llm"] is None
    assert recorder.debug_bound["embedder"] == "default"


@pytest.mark.asyncio
async def test_pipeline_breadcrumb_reflects_resolved_named_endpoints(monkeypatch):
    """The breadcrumb names the actually-resolved endpoint per stage, not just
    ``None`` — the named-endpoint counterpart to the all-disabled case above."""
    import services.workers.pipeline_builder as pb

    class _RecordingLogger:
        def __init__(self) -> None:
            self.bound: dict | None = None
            self.debug_bound: dict | None = None

        def bind(self, **kwargs):
            self.bound = kwargs
            return self

        def info(self, message: str) -> None:  # unused here, kept for parity
            pass

        def debug(self, message: str) -> None:
            self.debug_bound = self.bound

        def warning(self, message: str) -> None:
            pass

    recorder = _RecordingLogger()
    monkeypatch.setattr(pb, "logger", recorder)

    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[TextBlock(text="hello")],
        images=[ImageBlock(image_bytes=b"png")],
    )
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([Chunk(id="c1", text="hello", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=FakeVectorStore(),
        vlm_factory=lambda name: FakeVLM(),
        contextualizer_factory=lambda name: FakeContextualizer(),
        topic_tagger_factory=lambda name: FakeTopicTagger(),
    )
    row = {
        "task_id": "t1",
        "document": document,
        "partition": "tenant-a",
        "filename": "note.txt",
        "embedder_name": "embed-fast",
        "indexation_config": {
            "enable_image_captioning": True,
            "vlm": "vlm-fast",
            "enable_contextualization": True,
            "contextualization_llm": "ctx-llm",
            "enable_topic_tagging": True,
            "topic_tagging_llm": "topic-llm",
        },
    }

    await pipeline.run(row)

    assert recorder.debug_bound is not None
    assert recorder.debug_bound["vlm"] == "vlm-fast"
    assert recorder.debug_bound["contextualization_llm"] == "ctx-llm"
    assert recorder.debug_bound["topic_tagging_llm"] == "topic-llm"
    assert recorder.debug_bound["embedder"] == "embed-fast"


class RecordingVectorStore:
    """Vector store fake that logs call order so tests can assert the re-index
    insert-before-delete contract (#657)."""

    def __init__(self, existing_ids: list[str] | None = None) -> None:
        self.existing_ids = list(existing_ids or [])
        self.events: list[str] = []
        self.query_filters: list[dict] = []
        self.deleted: list[list[str]] = []
        self.collection_exists_result = True
        self.query_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def collection_exists(self, name: str) -> bool:
        return self.collection_exists_result

    async def query_ids_by_filter(self, collection: str, filters: dict) -> list[str]:
        self.events.append("query")
        self.query_filters.append(filters)
        if self.query_error is not None:
            raise self.query_error
        return list(self.existing_ids)

    async def upsert(self, chunks: list[Chunk], collection: str = "default", *, indexed_at=None) -> int:
        self.events.append("upsert")
        return len(chunks)

    async def ensure_collection(self, name: str, dimension: int, **kwargs) -> None:
        pass

    async def delete(self, ids: list[str], collection: str = "default") -> int:
        self.events.append("delete")
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(list(ids))
        return len(ids)


def _reindex_pipeline(vector_store, *, defer_replace_cleanup: bool = False):
    document = Document(filename="note.txt", text="hi", partition="tenant-a")
    processed = ProcessedDocument(document_id=document.id, text_blocks=[TextBlock(text="hi")])
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([Chunk(id="c1", text="hi", partition="tenant-a")]),
        embedder=FakeEmbedder([[1.0]]),
        vector_store=vector_store,
        defer_replace_cleanup=defer_replace_cleanup,
    )
    return pipeline, document


@pytest.mark.asyncio
async def test_reindex_direct_pipeline_deletes_prior_chunks_after_store():
    # #657: replace=True must snapshot the file's existing chunks, store the new
    # set, then delete exactly that old set for direct pipeline callers.
    vs = RecordingVectorStore(existing_ids=["101", "102"])
    pipeline, document = _reindex_pipeline(vs)

    row = await pipeline.run({"document": document, "partition": "tenant-a", "replace": True})

    # Snapshot is scoped to this file + partition only.
    assert vs.query_filters == [{"partition": "tenant-a", "file_id": document.id}]
    assert "_replace_old_chunk_collection" not in row
    assert "_replace_old_chunk_ids" not in row
    assert vs.deleted == [["101", "102"]]
    assert vs.events == ["query", "upsert", "delete"]


@pytest.mark.asyncio
async def test_reindex_deferred_pipeline_exposes_prior_chunks_for_worker_cleanup():
    # Worker indexing updates the catalog after storing vectors. In that path,
    # cleanup is deferred so a catalog failure cannot remove the old chunk set.
    vs = RecordingVectorStore(existing_ids=["101", "102"])
    pipeline, document = _reindex_pipeline(vs, defer_replace_cleanup=True)

    row = await pipeline.run({"document": document, "partition": "tenant-a", "replace": True})

    assert vs.query_filters == [{"partition": "tenant-a", "file_id": document.id}]
    assert row["_replace_old_chunk_collection"] == "default"
    assert row["_replace_old_chunk_ids"] == ["101", "102"]
    assert vs.deleted == []
    assert vs.events == ["query", "upsert"]


@pytest.mark.asyncio
async def test_reindex_no_delete_when_file_had_no_prior_chunks():
    vs = RecordingVectorStore(existing_ids=[])
    pipeline, document = _reindex_pipeline(vs)

    await pipeline.run({"document": document, "partition": "tenant-a", "replace": True})

    assert vs.query_filters == [{"partition": "tenant-a", "file_id": document.id}]
    assert vs.deleted == []
    assert "delete" not in vs.events


@pytest.mark.asyncio
async def test_first_time_index_never_queries_or_deletes():
    # Without replace, the plain insert path is untouched (no snapshot, no delete).
    vs = RecordingVectorStore(existing_ids=["999"])
    pipeline, document = _reindex_pipeline(vs)

    await pipeline.run({"document": document, "partition": "tenant-a"})

    assert vs.query_filters == []
    assert vs.deleted == []
    assert vs.events == ["upsert"]


@pytest.mark.asyncio
async def test_reindex_snapshot_failure_skips_cleanup_but_still_indexes():
    # A snapshot lookup failure must not lose the new indexing: duplicates are
    # recoverable, deleting blindly is not. Indexing completes; cleanup is skipped.
    vs = RecordingVectorStore(existing_ids=["1"])
    vs.query_error = RuntimeError("milvus unavailable")
    pipeline, document = _reindex_pipeline(vs)

    row = await pipeline.run({"document": document, "partition": "tenant-a", "replace": True})

    assert row["stage"] == "stored"
    assert "upsert" in vs.events
    assert vs.deleted == []


@pytest.mark.asyncio
async def test_reindex_defers_cleanup_delete_to_worker():
    # The pipeline must not delete stale chunks itself. The worker does that
    # after the catalog write, so a later catalog failure cannot leave the file
    # with neither the old nor the new vector set.
    vs = RecordingVectorStore(existing_ids=["1"])
    vs.delete_error = RuntimeError("delete failed")
    pipeline, document = _reindex_pipeline(vs, defer_replace_cleanup=True)

    row = await pipeline.run({"document": document, "partition": "tenant-a", "replace": True})

    assert row["stage"] == "stored"
    assert row["_replace_old_chunk_ids"] == ["1"]
    assert vs.events == ["query", "upsert"]


@pytest.mark.asyncio
async def test_reindex_with_zero_new_chunks_keeps_old_chunks():
    # Regression guard: a blank/whitespace-only file, or a parser that extracts
    # no text, legitimately chunks down to [] without raising (chunk_stage /
    # BaseChunker.chunk both treat this as success). If the old snapshot were
    # deleted whenever it was merely non-empty, a re-index that stores zero new
    # chunks would wipe the file's entire previous chunk set — worse than the
    # pre-fix duplication bug, and a violation of the "no empty window"
    # guarantee. The old chunks must survive when nothing new was stored.
    vs = RecordingVectorStore(existing_ids=["101", "102"])
    document = Document(filename="blank.txt", text="", partition="tenant-a")
    processed = ProcessedDocument(document_id=document.id, text_blocks=[])
    pipeline = build_indexing_pipeline(
        parser=FakeParser(processed),
        chunker=FakeChunker([]),
        embedder=FakeEmbedder([]),
        vector_store=vs,
    )

    row = await pipeline.run({"document": document, "partition": "tenant-a", "replace": True})

    assert row["stored_count"] == 0
    assert vs.deleted == []
    assert "delete" not in vs.events


def test_ingest_flag_defaults_come_from_the_model_not_from_absence():
    """A sparse indexation config that omits enable_image_captioning still
    captions during ingest (the model default is True). Reading the flag with a
    bare .get() treated that as disabled and skipped prompt resolution, leaving
    captioning silently on the disk seed while the preset named another prompt.
    """
    from services.workers.indexer_pool import _ingest_flag_default

    assert _ingest_flag_default("enable_image_captioning") is True
    assert _ingest_flag_default("enable_contextualization") is False
    assert _ingest_flag_default("enable_topic_tagging") is False
