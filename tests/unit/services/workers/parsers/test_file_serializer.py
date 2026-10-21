"""Covers ``ParserFileSerializer`` — the extractText / ``/extract`` path.

This serializer is the *second* caller of ``_caption_document``. It was left
fanning out unbounded when the per-document cap landed on the indexer path,
which is why ``_caption_document`` now requires ``max_concurrency`` explicitly.
These tests pin the propagation so that regression cannot come back quietly.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import core.config
import pytest
import services.workers.parsers.parser_dispatcher as parser_dispatcher
from core.models.document import Document, ImageBlock, ProcessedDocument, TextBlock
from services.workers.parsers.file_serializer import ParserFileSerializer
from services.workers.stages import caption as caption_module


@pytest.fixture(autouse=True)
def _noop_vlm_semaphore(monkeypatch):
    """Stub the cluster-wide VLM semaphore so captioning doesn't boot Ray."""

    @asynccontextmanager
    async def _noop():
        yield

    monkeypatch.setattr(caption_module, "get_vlm_semaphore", _noop)


class PeakTrackingVLM:
    """Records the peak number of caption calls running at once."""

    def __init__(self) -> None:
        self.calls = 0
        self.in_flight = 0
        self.peak = 0

    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            self.in_flight -= 1
        return "caption"


class FakeDispatcher:
    def __init__(self, processed: ProcessedDocument) -> None:
        self.processed = processed
        self.calls: list[Document] = []

    async def parse(self, document: Document) -> ProcessedDocument:
        self.calls.append(document)
        return self.processed


def _build_serializer(monkeypatch, vlm, *, vlm_semaphore: int | None, image_count: int = 20):
    processed = ProcessedDocument(
        document_id="doc-many-images",
        text_blocks=[TextBlock(text="body")],
        images=[ImageBlock(image_bytes=b"png") for _ in range(image_count)],
    )
    cfg = SimpleNamespace(
        loader=SimpleNamespace(image_captioning=True),
        semaphore=SimpleNamespace(vlm_semaphore=vlm_semaphore),
    )
    monkeypatch.setattr(core.config, "load_config", lambda: cfg)
    monkeypatch.setattr(parser_dispatcher, "build_parser_dispatcher", lambda _cfg, **_kw: FakeDispatcher(processed))
    monkeypatch.setattr(parser_dispatcher, "build_caption_vlm", lambda _cfg: vlm)
    monkeypatch.setattr(parser_dispatcher, "load_caption_prompt", lambda _cfg: "DESCRIBE")
    return ParserFileSerializer(), cfg


@pytest.mark.asyncio
async def test_serialize_bounds_caption_fan_out_at_the_configured_vlm_budget(monkeypatch, tmp_path):
    # The serializer must pass config.semaphore.vlm_semaphore down to
    # _caption_document. Pass None (or drop the argument) and 20 callers queue
    # on the shared VLM gate at once instead of 3.
    path = tmp_path / "album.docx"
    path.write_bytes(b"x")
    vlm = PeakTrackingVLM()
    serializer, cfg = _build_serializer(monkeypatch, vlm, vlm_semaphore=3)

    text = await serializer.serialize(str(path), {"filename": "album.docx"})

    assert serializer._caption_concurrency == cfg.semaphore.vlm_semaphore
    assert vlm.calls == 20
    assert vlm.peak <= 3
    # Captions are materialized into the returned text, so a caption stage that
    # silently no-opped could not satisfy this.
    assert text.startswith("body")
    assert text.count("caption") == 20


@pytest.mark.asyncio
async def test_serialize_leaves_caption_fan_out_unbounded_without_a_budget(monkeypatch, tmp_path):
    # Guards the test above against passing because captioning went serial.
    path = tmp_path / "album.docx"
    path.write_bytes(b"x")
    vlm = PeakTrackingVLM()
    serializer, _ = _build_serializer(monkeypatch, vlm, vlm_semaphore=None)

    await serializer.serialize(str(path), {"filename": "album.docx"})

    assert vlm.calls == 20
    assert vlm.peak > 3


@pytest.mark.asyncio
async def test_serialize_carries_the_files_own_path_on_the_document(monkeypatch, tmp_path):
    """#911: the extract path builds its own Document, so it needs the same
    ``source_path`` the indexer sets — otherwise a pooled parser reached through
    ``/extract`` still materialises a node-local temp file."""
    path = tmp_path / "album.docx"
    path.write_bytes(b"x")
    serializer, _ = _build_serializer(monkeypatch, PeakTrackingVLM(), vlm_semaphore=3)

    await serializer.serialize(str(path), {"filename": "album.docx"})

    dispatcher = serializer._dispatcher
    assert len(dispatcher.calls) == 1, "guard: the dispatcher must have been handed a document"
    assert dispatcher.calls[0].source_path == str(path)
