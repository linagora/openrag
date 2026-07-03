"""C1 — gpu-parser-serve worker.

Runs a parser ENGINE (``MarkerEngine`` today) as a :class:`TaskQueue` consumer,
**off Ray**. The same image is deployed in both modes; only the queue backend
and scaling differ (compose: fixed replicas; K8s: KEDA).

Job payload:  ``{"file_path": "<path on shared storage>"}``
Result:       ``{"markdown": str, "images": {key: base64-png}}``

Image handoff note: images are base64-inlined in the result for now — fine for
the single-L4 test and modest documents. **E1** swaps this for object-store keys
so large documents don't push big payloads through the broker.
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile

from core.config import Settings, load_config
from core.indexing.image_preprocessor import pil_to_png_bytes
from core.ports.task_queue import Task, TaskQueue
from core.utils.logging import get_logger
from di.messaging import build_task_queue
from services.workers.parsers.marker_engine import MAX_PDF_PAGES, MarkerEngine, create_chunks, page_count

logger = get_logger()

MARKER_TOPIC = "marker.parse"


async def parse_pdf(engine, config: Settings, file_path: str) -> tuple[str, dict]:
    """Chunk a PDF across the engine's executor and reassemble.

    Mirrors ``MarkerPool.process_pdf`` but with a single in-process engine, so
    output is identical to the Ray path — only the execution locus changes.
    """
    chunk_size = config.loader.marker_chunk_size
    total = page_count(file_path)
    capped = MAX_PDF_PAGES > 0 and total > MAX_PDF_PAGES
    pages = min(total, MAX_PDF_PAGES) if MAX_PDF_PAGES > 0 else total

    if chunk_size <= 0:
        page_range = list(range(pages)) if capped else None
        return await engine.process_pdf(file_path, page_range=page_range)

    chunks = create_chunks(pages, chunk_size)
    if len(chunks) == 1:
        page_range, _ = chunks[0]
        return await engine.process_pdf(file_path, page_range=(page_range if capped else None))

    results = await asyncio.gather(*[engine.process_pdf(file_path, page_range=pr) for pr, _ in chunks])
    all_markdown, all_images = [], {}
    for markdown, images in results:
        all_markdown.append(markdown)
        all_images.update(images)
    return "\n\n".join(all_markdown), all_images


def encode_images(images: dict) -> dict[str, str]:
    encoded: dict[str, str] = {}
    for key, pil_image in images.items():
        try:
            encoded[str(key)] = base64.b64encode(pil_to_png_bytes(pil_image)).decode()
        except Exception as exc:  # noqa: BLE001 — skip an unencodable image, keep the rest
            logger.warning(f"Failed to encode image {key}: {exc}")
    return encoded


class MarkerParseHandler:
    """TaskQueue handler: one whole-PDF parse per task. Model loaded once."""

    def __init__(self, config: Settings, engine=None):
        self.config = config
        self.engine = engine or MarkerEngine(config)

    async def __call__(self, task: Task) -> dict:
        payload = task.payload
        # Bytes-in-payload (app path, no shared FS) or a shared path (standalone test).
        if "file_bytes_b64" in payload:
            return await self._parse_bytes(base64.b64decode(payload["file_bytes_b64"]))
        markdown, images = await parse_pdf(self.engine, self.config, payload["file_path"])
        return {"markdown": markdown, "images": encode_images(images)}

    async def _parse_bytes(self, data: bytes) -> dict:
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(data)
            markdown, images = await parse_pdf(self.engine, self.config, path)
            return {"markdown": markdown, "images": encode_images(images)}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def close(self):
        close = getattr(self.engine, "close", None)
        if callable(close):
            close()


async def run_worker(config: Settings | None = None, *, concurrency: int | None = None) -> None:
    config = config or load_config()
    queue: TaskQueue = build_task_queue(config)
    handler = MarkerParseHandler(config)
    queue.register(MARKER_TOPIC, handler)
    n = concurrency if concurrency is not None else config.loader.marker_max_processes
    logger.info(f"marker-serve worker starting (backend={config.messaging.backend}, topic={MARKER_TOPIC}, concurrency={n})")
    try:
        await queue.run(concurrency=n)
    finally:
        handler.close()
        await queue.aclose()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
