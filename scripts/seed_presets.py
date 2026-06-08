#!/usr/bin/env python3
"""Seed default model endpoints and pipeline presets from the global Settings.

One-time migration utility for existing deployments upgrading to the Phase 14
DB-backed model-endpoint registry and per-partition preset system. It reads the
current YAML / env ``Settings`` and populates the ``model_endpoints``,
``pipeline_presets`` and ``partitions`` tables with their default rows so the
system keeps working without any admin interaction. Subsequent changes go
through the admin API.

The seeding itself lives in ``ServiceContainer.initialize()`` (the same path the
API runs on boot): it seeds endpoints, then presets, then the default partition,
and is idempotent — re-running skips anything already present. This script is a
thin operator entry point that runs that seed once, standalone (no API / Ray),
and prints a summary, so the migration can be performed without starting the
full app.

Usage::

    uv run python scripts/seed_presets.py
"""

from __future__ import annotations

import asyncio

from _bootstrap import ensure_openrag_source_path

ensure_openrag_source_path()

from core.config import load_config  # noqa: E402
from di.container import ServiceContainer  # noqa: E402


async def main() -> None:
    container = ServiceContainer(load_config())
    # initialize() runs the idempotent 3-phase seed: model endpoints → presets
    # → default partition (and loads each into the in-memory config).
    await container.initialize()
    try:
        config = container.config
        print(
            f"Seeded model endpoints: "
            f"{len(config.models.embedder)} embedder, "
            f"{len(config.models.reranker)} reranker, "
            f"{len(config.models.llm)} llm, "
            f"{len(config.models.vlm)} vlm"
        )
        print(
            f"Seeded {len(config.presets.indexation)} indexation presets, "
            f"{len(config.presets.retrieval)} retrieval presets"
        )
        print(f"Seeded {len(config.partitions)} partition(s)")
    finally:
        await container.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
