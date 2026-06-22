"""Run Postgres catalog migrations without starting the OpenRAG API."""

from __future__ import annotations

import asyncio

from core.config import load_config
from core.config.infrastructure import RDBConfig
from core.config.root import Settings
from services.persistence.connection import ConnectionManager


def _rdb_config_for_migrations(settings: Settings) -> RDBConfig:
    rdb = settings.rdb
    if rdb.database is not None:
        return rdb
    return rdb.model_copy(
        update={
            "database": f"partitions_for_collection_{settings.vectordb.collection_name}",
        }
    )


async def _run() -> None:
    manager = ConnectionManager(_rdb_config_for_migrations(load_config()))
    await manager.run_migrations()


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
