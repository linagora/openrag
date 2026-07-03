"""Smoke-test MarkerEngine on real models/GPU (verifies the B1 extraction).

Loads the real surya models and parses one PDF directly through the Ray-free
engine — no queue, no Ray. Confirms the extraction is behaviour-preserving on
real hardware (the one path that can't be unit-tested without a GPU).

Usage:
    PYTHONPATH=openrag uv run python scripts/smoke_marker_engine.py /path/to/sample.pdf
"""

import asyncio
import sys

from core.config import load_config
from services.workers.parsers.marker_engine import MarkerEngine


async def main(pdf: str) -> None:
    engine = MarkerEngine(load_config())  # loads models on the GPU
    try:
        markdown, images = await engine.process_pdf(pdf)
        print(f"OK — markdown chars: {len(markdown)} | images: {len(images)}")
        print("--- first 500 chars ---")
        print(markdown[:500])
    finally:
        engine.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_marker_engine.py <pdf>")
    asyncio.run(main(sys.argv[1]))
