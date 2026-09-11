from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from core.ports.document_repo import DocumentRepository
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


async def test_catalog_lookup_does_not_hide_outages():
    pool = AsyncMock()
    pool.fetch.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        await PgDocumentRepository(lambda: pool).get_indexed_documents({("a", "f")})


def test_repository_without_catalog_lookup_cannot_be_constructed():
    incomplete = type(
        "IncompleteRepository",
        (DocumentRepository,),
        {
            name: lambda *args, **kwargs: None
            for name in DocumentRepository.__abstractmethods__
            if name != "get_indexed_documents"
        },
    )
    with pytest.raises(TypeError, match="get_indexed_documents"):
        incomplete()
