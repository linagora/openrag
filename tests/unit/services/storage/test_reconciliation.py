from datetime import UTC, datetime, timedelta

import pytest
from services.storage.reconciliation import reconcile_partition

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=1)


class Catalog:
    def __init__(self, entries=()):
        self.entries = dict(entries)
        self.lookups = 0
        self.restore_on_recheck = None

    async def get_indexed_documents(self, keys):
        self.lookups += 1
        if self.lookups == 2 and self.restore_on_recheck:
            self.entries[self.restore_on_recheck] = OLD
        return {key: self.entries[key] for key in keys if key in self.entries}

    async def list_indexed_documents(self, partition, *, before, after=None, limit=500):
        return sorted(
            file_id
            for (p, file_id), timestamp in self.entries.items()
            if p == partition and timestamp < before and (after is None or file_id > after)
        )[:limit]


class Vectors:
    def __init__(self, rows):
        self.rows = rows
        self.deleted = []
        self.pages = 0
        self.closed = 0

    async def iter_chunk_metadata(self, collection, *, partition, file_ids=None, batch_size=500):
        rows = [r for r in self.rows if r["partition"] == partition and (file_ids is None or r["file_id"] in file_ids)]
        try:
            for offset in range(0, len(rows), batch_size):
                self.pages += 1
                yield rows[offset : offset + batch_size]
        finally:
            self.closed += 1

    async def delete(self, ids, collection):
        self.deleted.extend(ids)
        self.rows = [r for r in self.rows if str(r["_id"]) not in ids]
        return len(ids)


def row(id, file_id, timestamp=OLD, partition="a"):
    return {
        "_id": id,
        "file_id": file_id,
        "partition": partition,
        "indexed_at": timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp,
    }


async def run(catalog, vectors, **kwargs):
    return [
        event
        async for event in reconcile_partition(catalog, vectors, "collection", "a", now=NOW, page_size=2, **kwargs)
    ]


async def test_report_is_paged_read_only_and_partition_scoped():
    catalog = Catalog({("a", "live"): OLD, ("a", "missing"): OLD, ("a", "recent"): NOW, ("b", "orphan"): OLD})
    vectors = Vectors([row(1, "live"), row(2, "orphan"), row(3, "new", NOW), row(4, "orphan", partition="b")])
    events = await run(catalog, vectors)
    summary = events[-1]
    assert summary["type"] == "summary"
    assert summary["orphan_chunks"] == 1
    assert summary["missing_documents"] == 1
    assert summary["recent_chunks_skipped"] == 1
    assert [e["file_ids"] for e in events if e["type"] == "missing_chunks"] == [["missing"]]
    assert vectors.deleted == []
    assert vectors.pages >= 3


async def test_repair_deletes_only_aged_orphan_ids_and_rechecks_catalog():
    catalog = Catalog()
    catalog.restore_on_recheck = ("a", "restored")
    vectors = Vectors([row(1, "orphan"), row(2, "restored"), row(3, "young", NOW), row(4, "unknown", None)])
    events = await run(catalog, vectors, repair=True)
    assert vectors.deleted == ["1"]
    assert events[-1]["deleted_chunks"] == 1
    assert events[-1]["unaged_chunks"] == 1
    assert {r["_id"] for r in vectors.rows} == {2, 3, 4}


async def test_timestamp_mismatch_is_report_only_and_bad_timestamps_are_visible():
    catalog = Catalog({("a", "replaced"): OLD})
    vectors = Vectors([row(1, "replaced", OLD - timedelta(days=1)), row(2, "replaced"), row(3, "unknown", "bad")])
    events = await run(catalog, vectors, repair=True)
    assert events[-1]["timestamp_mismatches"] == 1
    assert events[-1]["unaged_chunks"] == 1
    assert vectors.deleted == []


async def test_catalog_failure_aborts_without_deleting_or_reporting_success():
    class FailedCatalog(Catalog):
        async def get_indexed_documents(self, keys):
            raise RuntimeError("catalog down")

    vectors = Vectors([row(1, "orphan")])
    with pytest.raises(RuntimeError, match="catalog down"):
        await run(FailedCatalog(), vectors, repair=True)
    assert vectors.deleted == []
    assert vectors.closed == 1


async def test_repair_recheck_failure_aborts_without_deletion():
    class FailedRecheck(Catalog):
        async def get_indexed_documents(self, keys):
            if self.lookups:
                raise RuntimeError("recheck failed")
            return await super().get_indexed_documents(keys)

    vectors = Vectors([row(1, "orphan")])
    with pytest.raises(RuntimeError, match="recheck failed"):
        await run(FailedRecheck(), vectors, repair=True)
    assert vectors.deleted == []
    assert vectors.closed == 1


@pytest.mark.parametrize("options", [{"page_size": 0}, {"page_size": 1001}, {"grace_seconds": -1}, {"partition": ""}])
async def test_rejects_invalid_scan_options(options):
    args = {"partition": "a", "page_size": 2, **options}
    with pytest.raises(ValueError):
        _ = [e async for e in reconcile_partition(Catalog(), Vectors([]), "collection", now=NOW, **args)]
