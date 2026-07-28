"""Run Postgres catalog migrations without starting the OpenRAG API."""

from __future__ import annotations

import asyncio

from core.config import load_config
from services.persistence.connection import ConnectionManager


async def _run() -> None:
    manager = ConnectionManager(load_config().resolved_rdb())
    await manager.run_migrations()


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
