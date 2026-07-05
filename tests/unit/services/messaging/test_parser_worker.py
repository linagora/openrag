"""C1 — gpu-parser-serve worker wiring (fake engine, no models/GPU).

Proves chunk/reassemble + image encoding + the full queue→handler→result path,
independent of Marker's models. The real model/GPU parse is the L4 smoke test.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from core.config.root import Settings
from core.ports.task_queue import Task, TaskStatus
from PIL import Image
from services.messaging import parser_worker
from services.messaging.in_memory import InMemoryTaskQueue
from services.messaging.parser_worker import MARKER_TOPIC, MarkerParseHandler
from services.object_store.memory import InMemoryObjectStore


class FakeEngine:
    def __init__(self):
        self.calls: list = []

    async def process_pdf(self, file_path, page_range=None):
        self.calls.append(page_range)
        start = page_range[0] if page_range else "all"
        return (f"chunk-{start}", {})

    def close(self):
        pass


def _cfg() -> Settings:
    return Settings()  # loader.marker_chunk_size defaults to 10


async def test_single_chunk(monkeypatch):
    monkeypatch.setattr(parser_worker, "page_count", lambda _p: 5)  # <= chunk_size
    handler = MarkerParseHandler(_cfg(), engine=FakeEngine())
    res = await handler(Task(topic=MARKER_TOPIC, payload={"file_path": "x.pdf"}))
    assert res["markdown"] == "chunk-all"
    assert res["images"] == {}


async def test_multi_chunk_reassembly_in_order(monkeypatch):
    monkeypatch.setattr(parser_worker, "page_count", lambda _p: 25)  # 3 chunks of 10
    engine = FakeEngine()
    handler = MarkerParseHandler(_cfg(), engine=engine)
    res = await handler(Task(topic=MARKER_TOPIC, payload={"file_path": "x.pdf"}))
    assert res["markdown"] == "chunk-0\n\nchunk-10\n\nchunk-20"
    assert sorted(c[0] for c in engine.calls) == [0, 10, 20]


async def test_images_are_base64_png(monkeypatch):
    monkeypatch.setattr(parser_worker, "page_count", lambda _p: 1)

    class ImgEngine:
        async def process_pdf(self, _fp, page_range=None):
            return ("md", {"_page_0_Picture_1.png": Image.new("RGB", (2, 2), "red")})

        def close(self):
            pass

    handler = MarkerParseHandler(_cfg(), engine=ImgEngine())
    res = await handler(Task(topic=MARKER_TOPIC, payload={"file_path": "x.pdf"}))
    assert "_page_0_Picture_1.png" in res["images"]
    raw = base64.b64decode(res["images"]["_page_0_Picture_1.png"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


async def test_object_key_branch_fetches_from_store(monkeypatch):
    monkeypatch.setattr(parser_worker, "page_count", lambda _p: 1)
    store = InMemoryObjectStore()
    await store.put("handoff/abc.pdf", b"%PDF fake bytes")
    handler = MarkerParseHandler(_cfg(), engine=FakeEngine(), object_store=store)
    res = await handler(Task(topic=MARKER_TOPIC, payload={"object_key": "handoff/abc.pdf"}))
    # Result is handed off to the store; only a key travels back through the broker.
    import json

    assert json.loads(await store.get(res["result_object_key"]))["markdown"] == "chunk-all"


async def test_object_key_without_store_raises():
    handler = MarkerParseHandler(_cfg(), engine=FakeEngine(), object_store=None)
    with pytest.raises(RuntimeError, match="requires an object store"):
        await handler(Task(topic=MARKER_TOPIC, payload={"object_key": "handoff/abc.pdf"}))


async def test_broken_engine_pool_is_reset_before_parse(monkeypatch):
    monkeypatch.setattr(parser_worker, "page_count", lambda _p: 1)

    class BrokenThenHealthyEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.broken = True
            self.reset_calls = 0

        def is_broken(self):
            return self.broken

        def reset(self):
            self.reset_calls += 1
            self.broken = False

    engine = BrokenThenHealthyEngine()
    handler = MarkerParseHandler(_cfg(), engine=engine)
    res = await handler(Task(topic=MARKER_TOPIC, payload={"file_path": "x.pdf"}))
    assert engine.reset_calls == 1  # broken pool was recovered, not left dead
    assert res["markdown"] == "chunk-all"


async def test_healthy_engine_pool_is_not_reset(monkeypatch):
    monkeypatch.setattr(parser_worker, "page_count", lambda _p: 1)

    class HealthyEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.reset_calls = 0

        def is_broken(self):
            return False

        def reset(self):
            self.reset_calls += 1

    engine = HealthyEngine()
    handler = MarkerParseHandler(_cfg(), engine=engine)
    await handler(Task(topic=MARKER_TOPIC, payload={"file_path": "x.pdf"}))
    assert engine.reset_calls == 0


async def test_end_to_end_through_queue(monkeypatch):
    monkeypatch.setattr(parser_worker, "page_count", lambda _p: 3)
    queue = InMemoryTaskQueue()
    handler = MarkerParseHandler(_cfg(), engine=FakeEngine())
    queue.register(MARKER_TOPIC, handler)
    run = asyncio.create_task(queue.run(concurrency=2))
    try:
        handle = await queue.submit(MARKER_TOPIC, {"file_path": "x.pdf"})
        res = await handle.result(timeout=5)
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await queue.aclose()

    assert res.status is TaskStatus.SUCCEEDED
    assert res.result["markdown"] == "chunk-all"
