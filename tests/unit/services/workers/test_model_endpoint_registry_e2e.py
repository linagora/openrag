from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from core.config.model_endpoints import ModelEndpointRow
from core.config.root import Settings
from core.models.chunk import Chunk
from core.models.document import Document, DocumentType, ImageBlock, ProcessedDocument, TextBlock
from services.orchestrators.model_endpoint_service import ModelEndpointService
from services.workers.indexer_pool import _build_embedder_factory, _build_vlm_factory
from services.workers.pipeline_builder import build_indexing_pipeline

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeEndpointRepo:
    def __init__(self, rows: list[ModelEndpointRow] | None = None) -> None:
        self._rows = rows or []

    async def list_all(self, model_type: str | None = None) -> list[ModelEndpointRow]:
        if model_type is None:
            return list(self._rows)
        return [row for row in self._rows if row.model_type == model_type]

    def replace(self, rows: list[ModelEndpointRow]) -> None:
        self._rows = rows


class _FakeParser:
    def __init__(self, processed: ProcessedDocument) -> None:
        self.processed = processed

    async def parse(self, document: Document) -> ProcessedDocument:
        return self.processed

    def supported_types(self) -> list[str]:
        return [DocumentType.TEXT.value]


class _FakeChunker:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks

    def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
        return self.chunks


class _DefaultEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def dimension(self) -> int:
        return 1

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[9.0] for _ in texts]

    async def embed_single(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]


class _DefaultVLM:
    def __init__(self) -> None:
        self.calls: list[bytes] = []

    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        self.calls.append(image_bytes)
        return "default caption"

    async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
        return [await self.caption_image(image, prompt=prompt) for image in images]


class _FakeVectorStore:
    def __init__(self) -> None:
        self.calls: list[tuple[list[Chunk], str]] = []

    async def ensure_collection(self, name: str, dimension: int, **kwargs: Any) -> None:
        return None

    async def upsert(self, chunks: list[Chunk], collection: str = "default", *, indexed_at=None) -> int:
        self.calls.append((chunks, collection))
        return len(chunks)


class _RecordingEmbedder:
    instances: list[_RecordingEmbedder] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[list[str]] = []
        self.instances.append(self)

    @property
    def dimension(self) -> int:
        return 1

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.25] for _ in texts]

    async def embed_single(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]


class _RecordingVLM:
    instances: list[_RecordingVLM] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[bytes] = []
        self.instances.append(self)

    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        self.calls.append(image_bytes)
        return "named caption"

    async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
        return [await self.caption_image(image, prompt=prompt) for image in images]


def _row(
    *,
    name: str,
    model_type: str,
    endpoint: str,
    model_name: str,
    implementation: str,
    api_key: str = "key-a",
    is_default: bool = False,
) -> ModelEndpointRow:
    return ModelEndpointRow(
        name=name,
        model_type=model_type,
        endpoint=endpoint,
        model_name=model_name,
        batch_size=7,
        timeout=11.0,
        extra={"implementation": implementation, "api_key": api_key},
        is_default=is_default,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _settings() -> Settings:
    return Settings(
        embedder={
            "base_url": "http://default-embedder/v1",
            "model_name": "default-embedder-model",
            "api_key": "default-embedder-key",
        },
        vlm={
            "base_url": "http://default-vlm/v1",
            "model": "default-vlm-model",
            "api_key": "default-vlm-key",
        },
    )


async def _hydrate(settings: Settings, repo: _FakeEndpointRepo) -> None:
    service = ModelEndpointService(model_endpoint_repo=repo, config=settings)
    await service.load_all()


async def _run_pipeline(
    *,
    settings: Settings,
    embedder_name: str | None = None,
    vlm_name: str | None = None,
    image_captioning: bool = False,
    default_embedder: _DefaultEmbedder | None = None,
    default_vlm: _DefaultVLM | None = None,
    embedder_factory: Any | None = None,
    vlm_factory: Any | None = None,
) -> tuple[dict[str, Any], _DefaultEmbedder, _DefaultVLM]:
    document = Document(filename="note.txt", text="hello", partition="tenant-a")
    processed = ProcessedDocument(
        document_id=document.id,
        text_blocks=[TextBlock(text="hello")],
        images=[ImageBlock(image_bytes=b"png")],
    )
    embedder = default_embedder or _DefaultEmbedder()
    vlm = default_vlm or _DefaultVLM()
    pipeline = build_indexing_pipeline(
        parser=_FakeParser(processed),
        chunker=_FakeChunker([Chunk(id="c1", text="hello", partition="tenant-a")]),
        embedder=embedder,
        vector_store=_FakeVectorStore(),
        vlm=vlm,
        embedder_factory=embedder_factory or _build_embedder_factory(settings),
        vlm_factory=vlm_factory or _build_vlm_factory(settings),
    )
    indexation_config: dict[str, Any] = {
        "enable_image_captioning": image_captioning,
        "enable_contextualization": False,
        "enable_topic_tagging": False,
    }
    if vlm_name is not None:
        indexation_config["vlm"] = vlm_name

    row: dict[str, Any] = {
        "document": document,
        "partition": "tenant-a",
        "filename": "note.txt",
        "indexation_config": indexation_config,
    }
    if embedder_name is not None:
        row["embedder_name"] = embedder_name

    await pipeline.run(row)
    return row, embedder, vlm


@pytest.fixture(autouse=True)
def _register_recording_components():
    from core.embeddings import embedder_registry
    from core.vlm import vlm_registry

    embedder_registry.register("e2e-recording-embedder")(_RecordingEmbedder)
    vlm_registry.register("e2e-recording-vlm")(_RecordingVLM)
    _RecordingEmbedder.instances.clear()
    _RecordingVLM.instances.clear()
    try:
        yield
    finally:
        embedder_registry._registry.pop("e2e-recording-embedder", None)
        vlm_registry._registry.pop("e2e-recording-vlm", None)
        _RecordingEmbedder.instances.clear()
        _RecordingVLM.instances.clear()


@pytest.mark.asyncio
async def test_indexing_uses_named_embedder_loaded_from_model_endpoint_registry():
    settings = _settings()
    embedder_factory = _build_embedder_factory(settings)
    repo = _FakeEndpointRepo(
        [
            _row(
                name="admin-embedder",
                model_type="embedder",
                endpoint="http://named-embedder/v1",
                model_name="named-embedder-model",
                implementation="e2e-recording-embedder",
            )
        ]
    )
    await _hydrate(settings, repo)

    row, default_embedder, _ = await _run_pipeline(
        settings=settings,
        embedder_name="admin-embedder",
        embedder_factory=embedder_factory,
    )

    assert default_embedder.calls == []
    assert len(_RecordingEmbedder.instances) == 1
    assert _RecordingEmbedder.instances[0].calls == [["hello"]]
    assert _RecordingEmbedder.instances[0].kwargs["endpoint"] == "http://named-embedder/v1"
    assert _RecordingEmbedder.instances[0].kwargs["model_name"] == "named-embedder-model"
    assert _RecordingEmbedder.instances[0].kwargs["api_key"] == "key-a"
    assert row["chunks"][0].embedding == [0.25]


@pytest.mark.asyncio
async def test_indexing_fails_for_missing_named_embedder_without_default_fallback():
    settings = _settings()
    embedder_factory = _build_embedder_factory(settings)
    await _hydrate(settings, _FakeEndpointRepo())
    default_embedder = _DefaultEmbedder()

    with pytest.raises(KeyError, match="Unknown embedder 'missing-embedder'"):
        await _run_pipeline(
            settings=settings,
            embedder_name="missing-embedder",
            default_embedder=default_embedder,
            embedder_factory=embedder_factory,
        )

    assert default_embedder.calls == []
    assert _RecordingEmbedder.instances == []


@pytest.mark.asyncio
async def test_indexing_uses_named_vlm_loaded_from_model_endpoint_registry():
    settings = _settings()
    vlm_factory = _build_vlm_factory(settings)
    repo = _FakeEndpointRepo(
        [
            _row(
                name="admin-vlm",
                model_type="vlm",
                endpoint="http://named-vlm/v1",
                model_name="named-vlm-model",
                implementation="e2e-recording-vlm",
            )
        ]
    )
    await _hydrate(settings, repo)

    row, _, default_vlm = await _run_pipeline(
        settings=settings,
        vlm_name="admin-vlm",
        image_captioning=True,
        vlm_factory=vlm_factory,
    )

    assert default_vlm.calls == []
    assert len(_RecordingVLM.instances) == 1
    assert _RecordingVLM.instances[0].calls == [b"png"]
    assert _RecordingVLM.instances[0].kwargs["endpoint"] == "http://named-vlm/v1"
    assert _RecordingVLM.instances[0].kwargs["model_name"] == "named-vlm-model"
    assert _RecordingVLM.instances[0].kwargs["api_key"] == "key-a"
    assert "named caption" in row["processed_document"].text_blocks[-1].text


@pytest.mark.asyncio
async def test_indexing_falls_back_to_default_vlm_when_named_vlm_is_missing():
    # A named VLM endpoint deleted/renamed after assignment must not fail the
    # indexing job — captioning falls back to the legacy default VLM with a
    # warning (parity with the contextualizer/topic-tagger selectors).
    settings = _settings()
    vlm_factory = _build_vlm_factory(settings)
    await _hydrate(settings, _FakeEndpointRepo())
    default_vlm = _DefaultVLM()

    row, _, returned_vlm = await _run_pipeline(
        settings=settings,
        vlm_name="missing-vlm",
        image_captioning=True,
        default_vlm=default_vlm,
        vlm_factory=vlm_factory,
    )

    assert returned_vlm is default_vlm
    assert default_vlm.calls == [b"png"]  # captioned via the legacy fallback
    assert _RecordingVLM.instances == []  # the named endpoint never resolved


@pytest.mark.asyncio
async def test_indexing_rebuilds_named_embedder_and_vlm_clients_after_endpoint_update():
    settings = _settings()
    embedder_factory = _build_embedder_factory(settings)
    vlm_factory = _build_vlm_factory(settings)
    repo = _FakeEndpointRepo(
        [
            _row(
                name="admin-embedder",
                model_type="embedder",
                endpoint="http://endpoint-a/v1",
                model_name="embedder-a",
                implementation="e2e-recording-embedder",
                api_key="key-a",
            ),
            _row(
                name="admin-vlm",
                model_type="vlm",
                endpoint="http://vlm-a/v1",
                model_name="vlm-a",
                implementation="e2e-recording-vlm",
                api_key="vlm-key-a",
            ),
        ]
    )
    await _hydrate(settings, repo)

    row_a, _, _ = await _run_pipeline(
        settings=settings,
        embedder_name="admin-embedder",
        vlm_name="admin-vlm",
        image_captioning=True,
        embedder_factory=embedder_factory,
        vlm_factory=vlm_factory,
    )

    repo.replace(
        [
            _row(
                name="admin-embedder",
                model_type="embedder",
                endpoint="http://endpoint-b/v1",
                model_name="embedder-b",
                implementation="e2e-recording-embedder",
                api_key="key-b",
            ),
            _row(
                name="admin-vlm",
                model_type="vlm",
                endpoint="http://vlm-b/v1",
                model_name="vlm-b",
                implementation="e2e-recording-vlm",
                api_key="vlm-key-b",
            ),
        ]
    )
    await _hydrate(settings, repo)

    row_b, _, _ = await _run_pipeline(
        settings=settings,
        embedder_name="admin-embedder",
        vlm_name="admin-vlm",
        image_captioning=True,
        embedder_factory=embedder_factory,
        vlm_factory=vlm_factory,
    )

    assert row_a["chunks"][0].embedding == [0.25]
    assert row_b["chunks"][0].embedding == [0.25]
    assert [instance.kwargs["endpoint"] for instance in _RecordingEmbedder.instances] == [
        "http://endpoint-a/v1",
        "http://endpoint-b/v1",
    ]
    assert [instance.kwargs["api_key"] for instance in _RecordingEmbedder.instances] == ["key-a", "key-b"]
    assert [instance.kwargs["endpoint"] for instance in _RecordingVLM.instances] == [
        "http://vlm-a/v1",
        "http://vlm-b/v1",
    ]
    assert [instance.kwargs["api_key"] for instance in _RecordingVLM.instances] == ["vlm-key-a", "vlm-key-b"]
