"""Submit a PDF to the marker-serve worker via the TaskQueue (exercises C1).

The full off-Ray path: this submits a job to the queue; the running
``parser_worker`` (marker-serve) consumes it, parses on the GPU, and returns the
result — no Ray. Worker + this script must share the queue backend/namespace and
(for now) the filesystem the PDF lives on.

Usage:
    QUEUE_BACKEND=nats NATS_URL=nats://localhost:4222 PYTHONPATH=openrag \
        uv run python scripts/submit_marker.py /path/to/sample.pdf
"""

import asyncio
import sys

from core.config import load_config
from di.messaging import build_task_queue


async def main(pdf: str) -> None:
    queue = build_task_queue(load_config())
    handle = await queue.submit("marker.parse", {"file_path": pdf})
    print(f"submitted task {handle.task_id}; waiting for result...")
    try:
        res = await handle.result(timeout=600)
        print("status:", res.status)
        if res.result:
            markdown = res.result.get("markdown", "")
            print(f"markdown chars: {len(markdown)} | images: {len(res.result.get('images', {}))}")
            print("--- first 500 chars ---")
            print(markdown[:500])
        if res.error:
            print("error:", res.error)
    finally:
        await queue.aclose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: submit_marker.py <pdf>")
    asyncio.run(main(sys.argv[1]))
