"""C1 — gpu-parser-serve worker wiring (fake engine, no models/GPU).

Proves chunk/reassemble + image encoding + the full queue→handler→result path,
independent of Marker's models. The real model/GPU parse is the L4 smoke test.
"""

from __future__ import annotations

import asyncio
import base64

from PIL import Image

from core.config.root import Settings
from core.ports.task_queue import Task, TaskStatus
from services.messaging import parser_worker
from services.messaging.in_memory import InMemoryTaskQueue
from services.messaging.parser_worker import MARKER_TOPIC, MarkerParseHandler


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
