import re

import pytest
from core.chunking.chunking_strategy import ChunkingStrategy
from core.embeddings.embedder import Embedder
from core.indexing.contextualize import ChunkContextualizer
from core.models.chunk import Chunk
from core.models.document import ImageBlock, ProcessedDocument, TextBlock
from core.prompts.vlm_prompt_builder import wrap_caption
from core.vector_stores.vector_store import VectorStore
from core.vlm.vlm import VLM
from services.workers.stages.caption import caption_stage
from services.workers.stages.chunk import chunk_stage
from services.workers.stages.contextualize import contextualize_stage
from services.workers.stages.embed import embed_stage
from services.workers.stages.store import store_stage


class FakeChunker(ChunkingStrategy):
    def __init__(self, chunks: list[Chunk], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.calls: list[tuple[ProcessedDocument, str]] = []

    def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
        self.calls.append((document, partition))
        if self.error is not None:
            raise self.error
        return self.chunks


class FakeContextualizer(ChunkContextualizer):
    def __init__(self, chunks: list[Chunk], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.calls: list[tuple[list[Chunk], str, str]] = []

    async def contextualize(self, chunks, *, filename: str = "", lang: str = "en") -> list[Chunk]:
        self.calls.append((list(chunks), filename, lang))
        if self.error is not None:
            raise self.error
        return self.chunks


class FakeEmbedder(Embedder):
    def __init__(self, vectors: list[list[float]], error: Exception | None = None) -> None:
        self.vectors = vectors
        self.error = error
        self.text_batches: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.text_batches.append(texts)
        if self.error is not None:
            raise self.error
        return self.vectors

    async def embed_single(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    @property
    def dimension(self) -> int:
        return 2


class FakeVLM(VLM):
    def __init__(self, captions: list[str], error: Exception | None = None) -> None:
        self.captions = captions
        self.error = error
        self.calls: list[tuple[bytes, str | None]] = []

    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        self.calls.append((image_bytes, prompt))
        if self.error is not None:
            raise self.error
        return self.captions[len(self.calls) - 1]

    async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
        return [await self.caption_image(image, prompt=prompt) for image in images]


class FakeVectorStore(VectorStore):
    def __init__(self, count: int, error: Exception | None = None) -> None:
        self.count = count
        self.error = error
        self.calls: list[tuple[list[Chunk], str]] = []
        self.ensure_calls: list[tuple[str, int]] = []

    async def upsert(self, chunks: list[Chunk], collection: str = "default") -> int:
        self.calls.append((chunks, collection))
        if self.error is not None:
            raise self.error
        return self.count

    async def search(
        self, embedding, query_text=None, top_k=10, collection="default", filters=None, similarity_threshold=None
    ):
        return []

    async def delete(self, ids: list[str], collection: str = "default") -> int:
        return 0

    async def ensure_collection(self, name: str, dimension: int, **kwargs) -> None:
        self.ensure_calls.append((name, dimension))
        return None

    async def drop_collection(self, name: str) -> None:
        return None

    async def collection_exists(self, name: str) -> bool:
        return True

    async def query_ids_by_filter(self, collection: str, filters: dict) -> list[str]:
        return []

    async def query_chunks_by_filter(self, collection: str, filters: dict, output_fields=None) -> list[dict]:
        return []


@pytest.mark.asyncio
async def test_caption_stage_replaces_markdown_refs_and_scrubs_credentials():
    markdown_ref = "![](image-1)"
    processed = ProcessedDocument(
        document_id="doc-1",
        text_blocks=[TextBlock(text=f"before {markdown_ref} after", page_number=2)],
        images=[ImageBlock(image_bytes=b"img", page_number=2, metadata={"markdown_ref": markdown_ref})],
    )
    row = {"processed_document": processed, "caption_prompt": "Describe", "token": "secret"}

    await caption_stage(row, FakeVLM(["a chart"]))

    assert row["processed_document"].images[0].caption == "a chart"
    assert row["processed_document"].text_blocks[0].text == f"before {wrap_caption('a chart')} after"
    assert row["stage"] == "captioned"
    assert "token" not in row


@pytest.mark.asyncio
async def test_caption_stage_appends_standalone_image_captions():
    processed = ProcessedDocument(
        document_id="doc-1",
        text_blocks=[TextBlock(text="body", page_number=1)],
        images=[ImageBlock(image_bytes=b"img", page_number=3)],
    )
    row = {"processed_document": processed}

    await caption_stage(row, FakeVLM(["a diagram"]))

    assert row["processed_document"].text_blocks[-1] == TextBlock(text=wrap_caption("a diagram"), page_number=3)
    assert row["stage"] == "captioned"


@pytest.mark.asyncio
async def test_chunk_stage_mutates_row_and_scrubs_credentials():
    processed = ProcessedDocument(document_id="doc-1", text_blocks=[TextBlock(text="hello")])
    chunks = [Chunk(id="c1", text="hello", partition="p1")]
    row = {"processed_document": processed, "partition": "p1", "api_key": "secret"}

    result = await chunk_stage(row, FakeChunker(chunks))

    assert result is row
    assert row["chunks"] == chunks
    assert row["stage"] == "chunked"
    assert "api_key" not in row


@pytest.mark.asyncio
async def test_contextualize_stage_uses_filename_and_language():
    chunks = [Chunk(id="c1", text="hello")]
    contextualized = [Chunk(id="c1", text="ctx hello", context="ctx")]
    contextualizer = FakeContextualizer(contextualized)
    row = {"chunks": chunks, "filename": "note.md", "language": "fr", "token": "secret"}

    await contextualize_stage(row, contextualizer)

    assert contextualizer.calls == [(chunks, "note.md", "fr")]
    assert row["chunks"] == contextualized
    assert row["stage"] == "contextualized"
    assert "token" not in row


@pytest.mark.asyncio
async def test_embed_stage_attaches_vectors_by_chunk_order():
    chunks = [Chunk(id="c1", text="alpha"), Chunk(id="c2", text="beta")]
    embedder = FakeEmbedder([[1.0, 0.0], [0.0, 1.0]])
    row = {"chunks": chunks, "secret": "value"}

    await embed_stage(row, embedder)

    assert embedder.text_batches == [["alpha", "beta"]]
    assert [chunk.embedding for chunk in row["chunks"]] == [[1.0, 0.0], [0.0, 1.0]]
    assert row["stage"] == "embedded"
    assert "secret" not in row


@pytest.mark.asyncio
async def test_store_stage_upserts_to_default_collection_with_chunk_partitions():
    chunks = [Chunk(id="c1", text="alpha", embedding=[1.0])]
    store = FakeVectorStore(count=1)
    row = {"chunks": chunks, "partition": "tenant-a", "credentials": {"token": "secret"}}

    await store_stage(row, store)

    assert store.ensure_calls == [("default", 1)]
    assert store.calls == [(chunks, "default")]
    assert row["stored_count"] == 1
    assert row["stage"] == "stored"
    assert "credentials" not in row


@pytest.mark.asyncio
async def test_store_stage_rejects_chunks_without_embeddings():
    chunks = [Chunk(id="c1", text="alpha")]
    store = FakeVectorStore(count=0)
    row = {"chunks": chunks}

    with pytest.raises(ValueError, match="without embeddings"):
        await store_stage(row, store)

    assert store.calls == []
    assert row["stage"] == "store_failed"


@pytest.mark.asyncio
async def test_stage_marks_error_and_scrubs_credentials_on_failure():
    row = {"chunks": [Chunk(id="c1", text="alpha")], "password": "secret"}

    with pytest.raises(RuntimeError, match="embed failed"):
        await embed_stage(row, FakeEmbedder([], error=RuntimeError("embed failed")))

    assert row["stage"] == "embed_failed"
    assert row["error"] == "embed failed"
    assert "password" not in row


@pytest.mark.asyncio
async def test_caption_stage_marks_error_and_scrubs_credentials_on_failure():
    processed = ProcessedDocument(
        document_id="doc-1",
        images=[ImageBlock(image_bytes=b"img", page_number=1)],
    )
    row = {"processed_document": processed, "api_key": "secret"}

    with pytest.raises(RuntimeError, match="caption failed"):
        await caption_stage(row, FakeVLM([], error=RuntimeError("caption failed")))

    assert row["stage"] == "caption_failed"
    assert row["error"] == "caption failed"
    assert "api_key" not in row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage_fn", "dependency", "expected_stage", "expected_error"),
    [
        (
            caption_stage,
            FakeVLM(["unused"]),
            "caption_failed",
            "caption_stage row must contain a ProcessedDocument under 'processed_document'",
        ),
        (
            chunk_stage,
            FakeChunker([]),
            "chunk_failed",
            "chunk_stage row must contain a ProcessedDocument under 'processed_document'",
        ),
        (
            contextualize_stage,
            FakeContextualizer([]),
            "contextualize_failed",
            "contextualize_stage row must contain a list[Chunk] under 'chunks'",
        ),
        (
            embed_stage,
            FakeEmbedder([]),
            "embed_failed",
            "embed_stage row must contain a list[Chunk] under 'chunks'",
        ),
        (
            store_stage,
            FakeVectorStore(count=0),
            "store_failed",
            "store_stage row must contain a list[Chunk] under 'chunks'",
        ),
    ],
)
async def test_stages_mark_error_and_scrub_credentials_on_invalid_input(
    stage_fn,
    dependency,
    expected_stage: str,
    expected_error: str,
):
    row = {"token": "secret"}

    with pytest.raises(ValueError, match=re.escape(expected_error)):
        await stage_fn(row, dependency)

    assert row["stage"] == expected_stage
    assert row["error"] == expected_error
    assert "token" not in row
