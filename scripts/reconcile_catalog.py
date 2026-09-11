#!/usr/bin/env python3
"""Report catalog/vector drift as NDJSON; optionally repair aged orphan chunks."""

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
    from services.storage.milvus_store import MilvusVectorStore
    from services.storage.reconciliation import reconcile_partition

    settings = load_config()
    # Diagnostics must not create databases, run migrations, seed defaults or
    # materialize a Milvus collection as a side effect of opening connections.
    rdb = settings.rdb.model_copy(
        update={
            "database": settings.rdb.database or f"partitions_for_collection_{settings.vectordb.collection_name}",
            "auto_create_database": False,
        }
    )
    connection = ConnectionManager(rdb)
    await connection.initialize()
    try:
        vectors = MilvusVectorStore(settings.vectordb)
        try:
            events = reconcile_partition(
                PgDocumentRepository(lambda: connection.pool),
                vectors,
                settings.vectordb.collection_name,
                args.partition,
                repair=args.repair,
                grace_seconds=args.grace_seconds,
                page_size=args.page_size,
            )
            async with aclosing(events):
                async for event in events:
                    yield event
        finally:
            await vectors.aclose()
    finally:
        await connection.shutdown()


async def _run(args) -> int:
    code = 0
    try:
        async with aclosing(scan(args)) as events:
            async for event in events:
                print(json.dumps(event), flush=True)
                if event["type"] == "summary" and any(
                    event.get(key, 0)
                    for key in (
                        "orphan_chunks",
                        "missing_documents",
                        "timestamp_mismatches",
                        "unaged_chunks",
                    )
                ):
                    code = 1
    except Exception as exc:
        print(json.dumps({"type": "error", "partition": args.partition, "message": str(exc)}), flush=True)
        return 2
    return code


def main(argv=None) -> int:
    from services.storage.reconciliation import validate_scan_options

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partition", required=True, help="One logical partition to inspect")
    parser.add_argument("--page-size", type=int, default=500, help="Rows per page (1..1000)")
    parser.add_argument("--grace-seconds", type=float, default=3600, help="Ignore indexing times younger than this age")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Delete aged orphan chunk IDs. Pause and drain all writers before using this flag.",
    )
    args = parser.parse_args(argv)
    try:
        validate_scan_options(args.partition, args.page_size, args.grace_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
