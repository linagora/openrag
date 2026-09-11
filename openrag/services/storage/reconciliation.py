"""Bounded, partition-scoped diagnostics for catalog/vector divergence."""

from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import Any

from core.ports.document_repo import DocumentRepository
from core.vector_stores import VectorStore


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(parsed, datetime) and parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    except ValueError:
        pass
    return None


async def reconcile_partition(
    catalog: DocumentRepository,
    vectors: VectorStore,
    collection: str,
    partition: str,
    *,
    repair: bool = False,
    grace_seconds: float = 3600,
    page_size: int = 500,
    now: datetime | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield NDJSON-ready findings followed by a summary on successful completion.

    Repair is for maintenance windows with writers paused. A fresh catalog
    lookup reduces races but cannot substitute for a shared admission fence.
    Only explicit, aged orphan IDs can be deleted. Timestamp mismatches are
    diagnostic: legacy imports and copies need not share an indexing timestamp.
    """
    if not partition or not 1 <= page_size <= 1000 or not 0 <= grace_seconds < float("inf"):
        raise ValueError("Provide a partition, page_size 1..1000 and a finite nonnegative grace_seconds")
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=grace_seconds)
    summary = {
        "type": "summary",
        "partition": partition,
        "repair": repair,
        "cutoff": cutoff.isoformat(),
        "scanned_chunks": 0,
        "scanned_documents": 0,
        "orphan_chunks": 0,
        "missing_documents": 0,
        "timestamp_mismatches": 0,
        "unaged_chunks": 0,
        "recent_chunks_skipped": 0,
        "deleted_chunks": 0,
        # A matching timestamp does not prove there is exactly one complete set.
        "duplicate_sets_checked": False,
    }
    pages = vectors.iter_chunk_metadata(collection, partition=partition, batch_size=page_size)
    async with aclosing(pages):
        async for page in pages:
            summary["scanned_chunks"] += len(page)
            existing = await catalog.get_indexed_documents({(partition, r["file_id"]) for r in page})
            orphans = []
            mismatches = []
            unaged = []
            for row in page:
                timestamp = _timestamp(row.get("indexed_at"))
                if timestamp is None:
                    unaged.append(str(row["_id"]))
                    continue
                if timestamp >= cutoff:
                    summary["recent_chunks_skipped"] += 1
                    continue
                key = (partition, row["file_id"])
                if key not in existing:
                    orphans.append(row)
                elif existing[key] < cutoff and timestamp != existing[key]:
                    mismatches.append(str(row["_id"]))
            if unaged:
                summary["unaged_chunks"] += len(unaged)
                yield {"type": "unknown_chunk_age", "partition": partition, "chunk_ids": unaged}
            if mismatches:
                summary["timestamp_mismatches"] += len(mismatches)
                yield {"type": "indexing_timestamp_mismatch", "partition": partition, "chunk_ids": mismatches}
            if orphans:
                summary["orphan_chunks"] += len(orphans)
                deleted = 0
                if repair:
                    current = await catalog.get_indexed_documents({(partition, r["file_id"]) for r in orphans})
                    ids = [str(r["_id"]) for r in orphans if (partition, r["file_id"]) not in current]
                    if ids:
                        deleted = await vectors.delete(ids, collection)
                        summary["deleted_chunks"] += deleted
                yield {
                    "type": "orphan_chunks",
                    "partition": partition,
                    "chunks": [{"chunk_id": str(r["_id"]), "file_id": r["file_id"]} for r in orphans],
                    "deleted_chunks": deleted,
                }

    after = None
    while True:
        file_ids = await catalog.list_indexed_documents(partition, before=cutoff, after=after, limit=page_size)
        if not file_ids:
            break
        summary["scanned_documents"] += len(file_ids)
        present = set()
        pages = vectors.iter_chunk_metadata(collection, partition=partition, file_ids=file_ids, batch_size=page_size)
        async with aclosing(pages):
            async for page in pages:
                present.update(r["file_id"] for r in page)
                if len(present) == len(file_ids):
                    break
        missing = [f for f in file_ids if f not in present]
        if missing:
            # A concurrent catalog delete is no longer a missing-vector finding.
            current = await catalog.get_indexed_documents({(partition, f) for f in missing})
            missing = [f for f in missing if (partition, f) in current and current[partition, f] < cutoff]
            if missing:
                summary["missing_documents"] += len(missing)
                yield {"type": "missing_chunks", "partition": partition, "file_ids": missing}
        after = file_ids[-1]
    yield summary
