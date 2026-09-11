from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from services.persistence.document_repo import PgDocumentRepository


async def test_batched_lookup_keeps_partition_file_pairs():
    pool = AsyncMock()
    timestamp = datetime(2026, 9, 1, tzinfo=UTC)
    pool.fetch.return_value = [{"partition_name": "a", "file_id": "f", "indexed_at": timestamp}]
    repo = PgDocumentRepository(lambda: pool)
    assert await repo.get_indexed_documents({("a", "f"), ("b", "g")}) == {("a", "f"): timestamp}
    query, partitions, file_ids = pool.fetch.call_args.args
    assert set(zip(partitions, file_ids)) == {("a", "f"), ("b", "g")}
    assert "unnest" in query.lower()
    pool.fetch.reset_mock()
    assert await repo.get_indexed_documents(set()) == {}
    pool.fetch.assert_not_awaited()


async def test_catalog_pages_use_age_and_exclusive_cursor():
    pool = AsyncMock()
    pool.fetch.return_value = [{"file_id": "next"}]
    repo = PgDocumentRepository(lambda: pool)
    before = datetime(2026, 9, 1, tzinfo=UTC)
    assert await repo.list_indexed_documents("a", before=before, after="last", limit=2) == ["next"]
    query, *params = pool.fetch.call_args.args
    assert params == ["a", before, "last", 2]
    assert "file_id > $3" in query
    assert "indexed_at < $2" in query


async def test_catalog_lookup_does_not_hide_outages():
    pool = AsyncMock()
    pool.fetch.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        await PgDocumentRepository(lambda: pool).get_indexed_documents({("a", "f")})
