"""D1/D3 — MarkerServeClient rebuilds a ProcessedDocument from a queue result,
and the dispatcher wires the marker_serve backend. In-memory queue, no models."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO

import pytest
from PIL import Image

from core.config.infrastructure import MessagingConfig
from core.config.root import Settings
from core.indexing.parsers.pdf.marker_serve import MarkerServeParser
from core.models.document import Document, DocumentType
from services.messaging.in_memory import InMemoryTaskQueue
from services.messaging.marker_serve_client import MarkerServeClient
from services.workers.parsers.parser_dispatcher import _PDF_BACKENDS, build_parser_dispatcher


def _png_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _run(queue):
    return asyncio.create_task(queue.run(concurrency=1))


async def test_parse_rebuilds_processed_document():
    queue = InMemoryTaskQueue()

    async def handler(task):
        assert "file_bytes_b64" in task.payload  # bytes handed off in the payload
        return {"markdown": "one{1}[PAGE_SEP]two{2}[PAGE_SEP]", "images": {"_page_0_Picture_1.png": _png_b64()}}

    queue.register("marker.parse", handler)
    run = await _run(queue)
    client = MarkerServeClient(Settings(), queue)
    doc = Document(id="d1", filename="x.pdf", content_type=DocumentType.PDF, raw_bytes=b"%PDF-1.4 fake")
    try:
        pd = await client.parse(doc)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await queue.aclose()

    assert pd.document_id == "d1"
    assert len(pd.text_blocks) >= 1
    assert len(pd.images) == 1
    assert pd.images[0].page_number == 1  # from "_page_0_..." → 1-indexed
    assert pd.images[0].image_bytes[:8] == b"\x89PNG\r\n\x1a\n"


async def test_empty_document_returns_empty():
    client = MarkerServeClient(Settings(), InMemoryTaskQueue())
    pd = await client.parse(Document(id="d2", content_type=DocumentType.PDF, raw_bytes=None))
    assert pd.document_id == "d2"
    assert pd.text_blocks == []


async def test_failed_result_raises():
    queue = InMemoryTaskQueue()

    async def handler(task):
        raise RuntimeError("boom")

    queue.register("marker.parse", handler)
    run = await _run(queue)
    # 1 attempt so it fails fast
    cfg = Settings()
    cfg_client = MarkerServeClient(cfg, queue)
    cfg_client._max_attempts = 1
    doc = Document(id="d3", content_type=DocumentType.PDF, raw_bytes=b"%PDF fake")
    try:
        with pytest.raises(RuntimeError, match="marker-serve parse failed"):
            await cfg_client.parse(doc)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await queue.aclose()


def test_backend_mapping_registered():
    assert _PDF_BACKENDS["MarkerServeLoader"] == "marker_serve"


def test_dispatcher_builds_marker_serve_facade():
    cfg = Settings(messaging=MessagingConfig(backend="in_memory"))
    dispatcher = build_parser_dispatcher(cfg)
    parser = dispatcher._get("marker_serve")  # builds facade + client (in-memory queue, no connect)
    assert isinstance(parser, MarkerServeParser)
