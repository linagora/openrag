#!/usr/bin/env python3
"""List uploaded originals no catalog row references, as NDJSON; optionally delete them."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import aclosing

from _bootstrap import ensure_openrag_source_path

ensure_openrag_source_path()


async def scan(args):
    from core.config import load_config
    from services.persistence.connection import ConnectionManager
    from services.persistence.document_repo import PgDocumentRepository
    from services.persistence.job_repo import PgJobRepository
    from services.storage.orphan_originals import scan_orphan_originals
    from services.storage.postgres_store import catalog_rdb_config

    settings = load_config()
    # A maintenance scan must not create a database or run migrations.
    rdb = catalog_rdb_config(settings).model_copy(update={"auto_create_database": False})
    connection = ConnectionManager(rdb)
    await connection.initialize()
    try:
        events = scan_orphan_originals(
            PgDocumentRepository(lambda: connection.pool),
            settings.paths.data_dir,
            min_age_seconds=args.min_age_hours * 3600,
            delete=args.delete,
            job_repo=PgJobRepository(lambda: connection.pool),
        )
        async with aclosing(events):
            async for event in events:
                yield event
    finally:
        await connection.shutdown()


async def _run(args) -> int:
    code = 0
    try:
        async with aclosing(scan(args)) as events:
            async for event in events:
                print(json.dumps(event), flush=True)
                if event["type"] == "summary" and event["orphans"]:
                    code = 1
    except Exception as exc:
        print(json.dumps({"type": "error", "message": str(exc)}), flush=True)
        return 2
    return code


def main(argv=None) -> int:
    from services.storage.orphan_originals import validate_min_age

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=24,
        help="Skip files younger than this (at least 1): an upload is on disk before its job writes its row.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the listed files, unless an indexing job old enough to own one hasn't settled.",
    )
    args = parser.parse_args(argv)
    try:
        validate_min_age(args.min_age_hours * 3600)
    except ValueError as exc:
        parser.error(str(exc))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
