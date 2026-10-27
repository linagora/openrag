import asyncio
import re
from contextlib import asynccontextmanager

import pytest
from core.chunking.chunking_strategy import ChunkingStrategy
from core.embeddings.embedder import Embedder
from core.indexing.contextualize import ChunkContextualizer
from core.models.chunk import Chunk
from core.models.document import Document, ImageBlock, ProcessedDocument, TextBlock
from core.prompts.vlm_prompt_builder import wrap_caption
from core.vector_stores.vector_store import VectorStore
from core.vlm.vlm import VLM
from services.workers.stages import _common as common_module
from services.workers.stages import caption as caption_module
from services.workers.stages._common import resolve_language
from services.workers.stages.caption import caption_stage
from services.workers.stages.chunk import chunk_stage
from services.workers.stages.contextualize import contextualize_stage
from services.workers.stages.embed import embed_stage
from services.workers.stages.store import store_stage


@pytest.fixture(autouse=True)
def _noop_vlm_semaphore(monkeypatch):
    """Stub the cluster-wide VLM semaphore so caption tests don't boot Ray."""

    @asynccontextmanager
    async def _noop():
        yield

    monkeypatch.setattr(caption_module, "get_vlm_semaphore", _noop)


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
        self.system_prompts: list[str | None] = []

    async def contextualize(
        self,
        chunks,
        *,
        filename: str = "",
        lang: str = "en",
        system_prompt: str | None = None,
        on_failure=None,
    ) -> list[Chunk]:
        self.calls.append((list(chunks), filename, lang))
        self.system_prompts.append(system_prompt)
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
    def iter_chunk_metadata(self, collection, *, partition, file_ids=None, batch_size=500):
        raise NotImplementedError("This write-only test double does not support reconciliation")

    def __init__(self, count: int, error: Exception | None = None) -> None:
        self.count = count
        self.error = error
        self.calls: list[tuple[list[Chunk], str]] = []
        self.ensure_calls: list[tuple[str, int]] = []
        self.vector_field_calls: list[tuple[str, int]] = []
        self.upsert_vector_fields: list[str | None] = []
        self.protected_metadata: dict | None = None

    async def upsert(
        self,
        chunks: list[Chunk],
        collection: str = "default",
        *,
        indexed_at=None,
        vector_field=None,
        protected_metadata=None,
    ) -> int:
        self.calls.append((chunks, collection))
        self.upsert_vector_fields.append(vector_field)
        self.protected_metadata = protected_metadata
        if self.error is not None:
            raise self.error
        return self.count

    async def search(
        self,
        embedding,
        query_text=None,
        top_k=10,
        collection="default",
        filters=None,
        similarity_threshold=None,
        vector_field=None,
    ):
        return []

    async def delete(self, ids: list[str], collection: str = "default") -> int:
        return 0

    async def delete_by_filter(self, filters: dict) -> int:
        return 0

    async def ensure_collection(self, name: str, dimension: int, **kwargs) -> None:
        self.ensure_calls.append((name, dimension))
        return None

    async def ensure_vector_field(self, field: str, dimension: int) -> bool:
        self.vector_field_calls.append((field, dimension))
        return True

    async def drop_vector_field(self, field: str) -> bool:
        return False

    async def write_vectors(self, field: str, vectors: dict[str, list[float] | None]) -> int:
        return 0

    async def drop_collection(self, name: str) -> None:
        return None

    async def collection_exists(self, name: str) -> bool:
        return True

    async def vector_dimension(self, vector_field: str | None = None) -> int | None:
        return 1024 if vector_field else None

    async def query_ids_by_filter(self, collection: str, filters: dict) -> list[str]:
        return []

    async def query_chunks_by_filter(
        self,
        collection: str,
        filters: dict,
        output_fields: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        return []


@pytest.mark.asyncio
async def test_fake_vector_store_accepts_filter_query_limit():
    store = FakeVectorStore(count=0)

    assert await store.query_chunks_by_filter("collection", {}, limit=1) == []


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
async def test_caption_stage_squeezes_table_padding():
    # A degenerate table caption repeats padded rows up to its token budget;
    # the padding is dropped before the caption reaches chunking and storage.
    padded = "| Plouray         |             |\n" * 3
    processed = ProcessedDocument(document_id="doc-1", images=[ImageBlock(image_bytes=b"img", page_number=1)])
    row = {"processed_document": processed}

    await caption_stage(row, FakeVLM([padded]))

    squeezed = "| Plouray | |\n" * 3
    assert row["processed_document"].images[0].caption == squeezed
    assert row["processed_document"].text_blocks[-1].text == wrap_caption(squeezed)


def test_squeeze_padding_only_changes_markdown_table_rows():
    caption = (
        "This  prose remains  as written.\n"
        "| Header        | Value          |\n"
        "| ------------- | -------------- |\n"
        "| padded   name | padded     row |\n"
    )

    assert caption_module.squeeze_padding(caption) == (
        "This  prose remains  as written.\n"
        "| Header | Value |\n"
        "| ------------- | -------------- |\n"
        "| padded name | padded row |\n"
    )


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        pytest.param(
            "| Name     | Value     |\n| --- | --- |\n| short     |\n| extra     | cells     | here     |\n| next     | row     |\n",
            "| Name | Value |\n| --- | --- |\n| short |\n| extra | cells | here |\n| next | row |\n",
            id="varying-body-widths",
        ),
        pytest.param(
            "Name     | Value\n--- | ---\nfirst     | second\n",
            "Name | Value\n--- | ---\nfirst | second\n",
            id="without-outer-pipes",
        ),
        pytest.param(
            "Name     | Value\r\n| --- | --- |\r\n| first     | second\r\nnext     | row |\r\n",
            "Name | Value\r\n| --- | --- |\r\n| first | second\r\nnext | row |\r\n",
            id="mixed-outer-pipes-and-crlf",
        ),
        pytest.param(
            "| Name     |\n| --- |\n| padded     value |\n",
            "| Name |\n| --- |\n| padded value |\n",
            id="single-column",
        ),
        pytest.param(
            "Name     | Value\n--- | ---\nfirst  \\| second | third     value\n",
            "Name | Value\n--- | ---\nfirst \\| second | third value\n",
            id="escaped-pipe",
        ),
        pytest.param(
            "`a    b` | Value\n--- | ---\nfirst     | second\n",
            "`a    b` | Value\n--- | ---\nfirst | second\n",
            id="inline-code-header",
        ),
        pytest.param(
            "Pipe     diagram | keep     spacing\n| Name     | Value |\n| --- | --- |\n| first     | second |\n",
            "Pipe     diagram | keep     spacing\n| Name | Value |\n| --- | --- |\n| first | second |\n",
            id="preserve-preceding-pipe-prose",
        ),
        pytest.param(
            "| Name | Value |\n| --- | --- |\n١. first     | second\n| next     | row     |\n",
            "| Name | Value |\n| --- | --- |\n١. first | second\n| next | row |\n",
            id="unicode-digit-is-not-a-list-marker",
        ),
    ],
)
def test_squeeze_padding_supports_markdown_table_layouts(caption, expected):
    assert caption_module.squeeze_padding(caption) == expected


@pytest.mark.parametrize("delimiter", ["| - | - |", "|:--|--:|", "- | -"])
def test_squeeze_padding_supports_short_delimiters(delimiter):
    caption = f"| Name     | Value     |\n{delimiter}\n| first     | second     |\n"

    assert caption_module.squeeze_padding(caption) == f"| Name | Value |\n{delimiter}\n| first | second |\n"


@pytest.mark.parametrize(
    ("body", "expected_body"),
    [("short", "short"), ("short     row", "short row"), ("`a    b`", "`a    b`")],
)
def test_squeeze_padding_continues_after_body_rows_without_pipes(body, expected_body):
    caption = f"| Name     | Value     |\n| --- | --- |\n{body}\n| next     | row     |\n"

    assert caption_module.squeeze_padding(caption) == (
        f"| Name | Value |\n| --- | --- |\n{expected_body}\n| next | row |\n"
    )


@pytest.mark.parametrize(
    "block",
    [
        ">     foo    |    bar\n" * 2,
        "   >     foo    |    bar\n" * 2,
        "# Heading    |    text\n",
        "- List    |    item\n" * 2,
        "1. List    |    item\n" * 2,
        "\nProse    |    text\n",
        "---\nProse    |    text\n",
        "<pre>\n    foo    |    bar\n</pre>\n",
        ' \tprint("foo    bar")\n',
        '  \tprint("foo    bar")\n',
        '   \tprint("foo    bar")\n',
    ],
)
def test_squeeze_padding_stops_at_other_markdown_blocks(block):
    table = "| Name     | Value     |\n| --- | --- |\n| first     | second     |\n"

    assert caption_module.squeeze_padding(table + block) == (
        "| Name | Value |\n| --- | --- |\n| first | second |\n" + block
    )


@pytest.mark.parametrize(
    "caption",
    [
        "|     A     |\n" * 3,
        "| Name     | Value     |\n| --- |\n| untouched     |\n",
        "Name     | Value\n--- | --- | ---\nfirst     | second\n",
    ],
)
def test_squeeze_padding_preserves_rows_without_a_valid_table_header(caption):
    assert caption_module.squeeze_padding(caption) == caption


def test_squeeze_padding_preserves_pipe_diagrams():
    caption = "|     A     |\n|     A     |     B     |\n"

    assert caption_module.squeeze_padding(caption) == caption


def test_squeeze_padding_preserves_preformatted_markdown():
    caption = (
        "The column `Total` lists sums.\n"
        "- Regions\n"
        "    - Bretagne\n"
        "    \n"
        "| name             | notes        |\n"
        "|---|---|\n"
        "| value      | `a    b`  |\n"
        "```\n"
        "| code    | data      |\n"
        "```\n"
        "~~~text\n"
        "| tilde    | data      |\n"
        "~~~\n"
        "    | x    | y    |\n"
        "This  prose remains  as written.\n"
    )

    expected = (
        "The column `Total` lists sums.\n"
        "- Regions\n"
        "    - Bretagne\n"
        "    \n"
        "| name | notes |\n"
        "|---|---|\n"
        "| value      | `a    b`  |\n"
        "```\n"
        "| code    | data      |\n"
        "```\n"
        "~~~text\n"
        "| tilde    | data      |\n"
        "~~~\n"
        "    | x    | y    |\n"
        "This  prose remains  as written.\n"
    )

    assert caption_module.squeeze_padding(caption) == expected


@pytest.mark.asyncio
async def test_caption_stage_captions_images_concurrently_and_in_order():
    """Images fan out concurrently (bounded only by the VLM semaphore, stubbed
    here), and each caption lands on its own image even though later images
    finish their VLM call first."""

    class TrackingVLM(VLM):
        def __init__(self) -> None:
            self.in_flight = 0
            self.max_in_flight = 0

        async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                # Earlier images sleep longer, so completion order is the
                # reverse of submission order — a sequential loop would caption
                # in submission order and never overlap.
                idx = int(image_bytes.decode().rsplit("-", 1)[1])
                await asyncio.sleep((3 - idx) * 0.01)
                return f"caption:{image_bytes.decode()}"
            finally:
                self.in_flight -= 1

        async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
            return [await self.caption_image(image, prompt=prompt) for image in images]

    processed = ProcessedDocument(
        document_id="doc-1",
        text_blocks=[TextBlock(text="body", page_number=1)],
        images=[ImageBlock(image_bytes=f"img-{i}".encode(), page_number=1) for i in range(3)],
    )
    row = {"processed_document": processed}
    vlm = TrackingVLM()

    await caption_stage(row, vlm)

    out = row["processed_document"]
    assert [img.caption for img in out.images] == ["caption:img-0", "caption:img-1", "caption:img-2"]
    # All three VLM calls were in flight simultaneously: the fan-out is real.
    assert vlm.max_in_flight == 3
    assert row["stage"] == "captioned"


@pytest.mark.asyncio
async def test_caption_stage_cancels_sibling_captions_on_failure():
    """Do not leave background VLM calls running after one caption fails."""

    class FailingVLM(VLM):
        def __init__(self) -> None:
            self.started: set[bytes] = set()
            self.cancelled: set[bytes] = set()
            self.completed: set[bytes] = set()
            self.all_started = asyncio.Event()
            self.release = asyncio.Event()

        async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
            self.started.add(image_bytes)
            if len(self.started) == 3:
                self.all_started.set()

            if image_bytes == b"fail":
                await self.all_started.wait()
                raise RuntimeError("caption failed")

            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.add(image_bytes)
                raise
            self.completed.add(image_bytes)
            return image_bytes.decode()

        async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
            return [await self.caption_image(image, prompt=prompt) for image in images]

    processed = ProcessedDocument(
        document_id="doc-1",
        text_blocks=[TextBlock(text="body", page_number=1)],
        images=[
            ImageBlock(image_bytes=b"slow-1", page_number=1),
            ImageBlock(image_bytes=b"fail", page_number=1),
            ImageBlock(image_bytes=b"slow-2", page_number=1),
        ],
    )
    row = {"processed_document": processed}
    vlm = FailingVLM()

    try:
        with pytest.raises(RuntimeError, match="caption failed"):
            await caption_stage(row, vlm)

        assert vlm.cancelled == {b"slow-1", b"slow-2"}
        assert vlm.completed == set()
    finally:
        vlm.release.set()
        await asyncio.sleep(0)


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
async def test_contextualize_stage_detects_the_language_when_the_upload_names_none(monkeypatch):
    # Without upload metadata the stage used to pass "en", which overrode the
    # prompt's "write in the chunk's language" and wrote English contexts for
    # French corpora.
    seen: dict = {}

    def fake_detect(text, *, min_confidence=0.0):
        seen["text"] = text
        return "fr"

    monkeypatch.setattr(common_module, "detect_language", fake_detect)
    chunks = [Chunk(id="c1", text="La préfecture de région répartit les crédits.")]
    contextualizer = FakeContextualizer([Chunk(id="c1", text="ctx", context="ctx")])
    row = {"chunks": chunks, "filename": "note.pdf", "language": None}

    await contextualize_stage(row, contextualizer)

    assert contextualizer.calls == [(chunks, "note.pdf", "fr")]
    assert row["language"] == "fr"
    assert "préfecture" in seen["text"]


def test_resolve_language_prefers_the_upload_metadata(monkeypatch):
    monkeypatch.setattr(common_module, "detect_language", lambda *a, **k: pytest.fail("must not detect"))
    assert resolve_language({"language": "de", "chunks": []}) == "de"


def test_resolve_language_falls_back_to_english_when_detection_is_unsure(monkeypatch):
    monkeypatch.setattr(common_module, "detect_language", lambda *a, **k: None)
    row = {"chunks": [Chunk(id="c1", text="12 34 56")]}
    assert resolve_language(row) == "en"
    assert row["language"] == "en"


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
async def test_store_stage_passes_catalog_metadata_for_upload_fitting():
    chunks = [Chunk(id="c1", text="alpha", embedding=[1.0])]
    store = FakeVectorStore(count=1)
    document = Document(id="file-1", filename="a.pdf", metadata={"description": "catalog value"})

    await store_stage({"document": document, "chunks": chunks}, store)

    assert store.protected_metadata == document.metadata


@pytest.mark.asyncio
async def test_store_stage_stamps_task_id_on_chunks_for_precise_cleanup():
    chunks = [Chunk(id="c1", text="alpha", embedding=[1.0])]
    store = FakeVectorStore(count=1)
    row = {"task_id": "task-1", "chunks": chunks}

    await store_stage(row, store)

    assert chunks[0].metadata["_openrag_indexing_task_id"] == "task-1"


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


class _ConcurrencyTrackingVLM(VLM):
    """Records the peak number of caption calls running at once."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0

    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            self.in_flight -= 1
        return "caption"

    async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
        return [await self.caption_image(image, prompt=prompt) for image in images]


def _document_with_images(count: int) -> ProcessedDocument:
    return ProcessedDocument(
        document_id="doc-many-images",
        text_blocks=[TextBlock(text="body", page_number=1)],
        images=[ImageBlock(image_bytes=b"img", page_number=1) for _ in range(count)],
    )


@pytest.mark.asyncio
async def test_caption_stage_bounds_per_document_fan_out():
    vlm = _ConcurrencyTrackingVLM()
    row = {"processed_document": _document_with_images(20)}

    await caption_stage(row, vlm, max_concurrency=3)

    assert vlm.peak <= 3
    assert len(row["processed_document"].images) == 20
    assert all(image.caption == "caption" for image in row["processed_document"].images)


@pytest.mark.asyncio
async def test_caption_stage_fan_out_is_unbounded_without_a_limit():
    # Guards the test above against passing for the wrong reason (e.g. the stage
    # having become serial), and preserves the previous default.
    vlm = _ConcurrencyTrackingVLM()
    row = {"processed_document": _document_with_images(20)}

    await caption_stage(row, vlm)

    assert vlm.peak > 3


@pytest.mark.asyncio
async def test_store_stage_provisions_the_partitions_field_then_writes_to_it():
    # Sized from the embedding actually produced.
    chunks = [Chunk(id="c1", text="alpha", embedding=[1.0, 0.0, 1.0])]
    store = FakeVectorStore(count=1)
    calls: list[str] = []
    ensure, upsert = store.ensure_vector_field, store.upsert

    async def recording_ensure(*args, **kwargs):
        calls.append("ensure")
        return await ensure(*args, **kwargs)

    async def recording_upsert(*args, **kwargs):
        calls.append("upsert")
        return await upsert(*args, **kwargs)

    store.ensure_vector_field, store.upsert = recording_ensure, recording_upsert  # type: ignore[method-assign]

    await store_stage({"chunks": chunks}, store, vector_field="vector_bge_m3")

    assert calls == ["ensure", "upsert"]
    assert store.vector_field_calls == [("vector_bge_m3", 3)]
    assert store.upsert_vector_fields == ["vector_bge_m3"]


def test_resolve_language_ignores_digits_and_table_markup(monkeypatch):
    seen: dict = {}

    def fake_detect(text, *, min_confidence=0.0):
        seen["text"] = text
        return "fr"

    monkeypatch.setattr(common_module, "detect_language", fake_detect)
    resolve_language({"chunks": [Chunk(id="c1", text="| Montant | 58.3M |\n|---|---|\n| Dossiers acceptés | 22 |")]})
    assert seen["text"] == "Montant . M Dossiers acceptés"
