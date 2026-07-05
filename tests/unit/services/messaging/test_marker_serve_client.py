"""D1/D3 + E1 — MarkerServeClient uploads the PDF to the object store, hands the
worker only the key, rebuilds a ProcessedDocument from the result, and cleans up
the object. In-memory queue + in-memory object store, no models."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from io import BytesIO

import pytest
from core.config.infrastructure import MessagingConfig, ObjectStoreConfig
from core.config.root import Settings
from core.indexing.parsers.pdf.marker_serve import MarkerServeParser
from core.models.document import Document, DocumentType
from PIL import Image
from services.messaging.in_memory import InMemoryTaskQueue
from services.messaging.marker_serve_client import MarkerServeClient
from services.object_store.memory import InMemoryObjectStore
from services.workers.parsers.parser_dispatcher import _PDF_BACKENDS, build_parser_dispatcher

PDF_BYTES = b"%PDF-1.4 fake"
EXPECTED_KEY = f"handoff/{hashlib.sha256(PDF_BYTES).hexdigest()}.pdf"


def _png_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _run(queue):
    return asyncio.create_task(queue.run(concurrency=1))


async def test_parse_uploads_by_key_and_rebuilds_document():
    queue = InMemoryTaskQueue()
    store = InMemoryObjectStore()
    seen: dict = {}

    async def handler(task):
        # Worker side: only a key travels in the payload; fetch the bytes back.
        assert "file_bytes_b64" not in task.payload
        key = task.payload["object_key"]
        seen["bytes"] = await store.get(key)  # proves the client uploaded them
        return {"markdown": "one{1}[PAGE_SEP]two{2}[PAGE_SEP]", "images": {"_page_0_Picture_1.png": _png_b64()}}

    queue.register("marker.parse", handler)
    run = await _run(queue)
    client = MarkerServeClient(Settings(), queue, store)
    doc = Document(id="d1", filename="x.pdf", content_type=DocumentType.PDF, raw_bytes=PDF_BYTES)
    try:
        pd = await client.parse(doc)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await queue.aclose()

    assert seen["bytes"] == PDF_BYTES  # worker received the exact bytes via the store
    assert pd.document_id == "d1"
    assert len(pd.text_blocks) >= 1
    assert len(pd.images) == 1
    assert pd.images[0].page_number == 1  # from "_page_0_..." → 1-indexed
    assert pd.images[0].image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    # The client does NOT delete the object (that would race a retrying worker);
    # a bucket TTL reaps it, so it is still present right after parse().
    assert await store.get(EXPECTED_KEY) == PDF_BYTES


async def test_empty_document_uploads_nothing():
    store = InMemoryObjectStore()
    client = MarkerServeClient(Settings(), InMemoryTaskQueue(), store)
    pd = await client.parse(Document(id="d2", content_type=DocumentType.PDF, raw_bytes=None))
    assert pd.document_id == "d2"
    assert pd.text_blocks == []
    assert store._objects == {}  # short-circuits before any upload


async def test_failed_result_raises_and_still_cleans_up():
    queue = InMemoryTaskQueue()
    store = InMemoryObjectStore()

    async def handler(task):
        raise RuntimeError("boom")

    queue.register("marker.parse", handler)
    run = await _run(queue)
    client = MarkerServeClient(Settings(), queue, store)
    client._max_attempts = 1  # fail fast
    doc = Document(id="d3", content_type=DocumentType.PDF, raw_bytes=PDF_BYTES)
    try:
        with pytest.raises(RuntimeError, match="marker-serve parse failed"):
            await client.parse(doc)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await queue.aclose()

    # Client never deletes the object (TTL reaps it); it persists after a failure.
    assert EXPECTED_KEY in store._objects


async def test_timeout_leaves_object_for_retrying_worker():
    # No consumer runs, so the result times out while the task is still pending.
    # The client must NOT delete the object — a later worker attempt needs it.
    queue = InMemoryTaskQueue()
    store = InMemoryObjectStore()
    client = MarkerServeClient(Settings(), queue, store)
    client._timeout = 0.2
    doc = Document(id="d4", content_type=DocumentType.PDF, raw_bytes=PDF_BYTES)
    with pytest.raises(TimeoutError):
        await client.parse(doc)
    assert await store.get(EXPECTED_KEY) == PDF_BYTES  # preserved for the retry
    await queue.aclose()


def test_backend_mapping_registered():
    assert _PDF_BACKENDS["MarkerServeLoader"] == "marker_serve"


def test_dispatcher_builds_marker_serve_facade():
    cfg = Settings(
        messaging=MessagingConfig(backend="in_memory"),
        object_store=ObjectStoreConfig(backend="in_memory"),
    )
    dispatcher = build_parser_dispatcher(cfg)
    parser = dispatcher._get("marker_serve")  # builds facade + client (in-memory, no connect)
    assert isinstance(parser, MarkerServeParser)
