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


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class _StreamingConnection:
    def __init__(self, rows):
        self.rows = rows
        self.transaction_kwargs = None
        self.cursor_call = None

    def transaction(self, **kwargs):
        self.transaction_kwargs = kwargs
        return _AsyncContext(None)

    def cursor(self, query, partition, *, prefetch):
        self.cursor_call = (query, partition, prefetch)

        async def rows():
            for row in self.rows:
                yield row

        return rows()


class _StreamingPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


async def test_indexed_corpus_state_streams_one_consistent_ordered_snapshot():
    timestamp = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [
        {
            "file_id": "file-a",
            "indexed_at": timestamp,
            "content_sha256": "content-a",
            "chunk_count": 3,
            "relationship_id": "relationship-a",
            "parent_id": "parent-a",
        }
    ]
    connection = _StreamingConnection(rows)
    pool = _StreamingPool(connection)
    repo = PgDocumentRepository(lambda: pool)

    first = await repo.get_indexed_corpus_state("a", document_ids_limit=1)
    rows[0]["relationship_id"] = "relationship-b"
    relationship_changed = await repo.get_indexed_corpus_state("a", document_ids_limit=1)
    rows[0]["parent_id"] = "parent-b"
    parent_changed = await repo.get_indexed_corpus_state("a", document_ids_limit=1)

    assert first.count == 1
    assert len(first.digest) == 64
    assert first.document_ids == ("file-a",)
    assert first.document_ids_truncated is False
    assert first.digest != relationship_changed.digest
    assert relationship_changed.digest != parent_changed.digest
    query, partition, prefetch = connection.cursor_call
    assert partition == "a"
    assert prefetch == 1000
    assert "ORDER BY file_id" in query
    assert "LIMIT" not in query
    assert "relationship_id" in query
    assert "parent_id" in query
    assert connection.transaction_kwargs == {"isolation": "repeatable_read", "readonly": True}


async def test_catalog_lookup_does_not_hide_outages():
    pool = AsyncMock()
    pool.fetch.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        await PgDocumentRepository(lambda: pool).get_indexed_documents({("a", "f")})


@pytest.mark.parametrize(
    "method",
    ["get_indexed_documents", "list_indexed_documents", "get_indexed_corpus_state"],
)
def test_repository_without_catalog_lookup_cannot_be_constructed(method):
    incomplete = type(
        "IncompleteRepository",
        (DocumentRepository,),
        {name: lambda *args, **kwargs: None for name in DocumentRepository.__abstractmethods__ if name != method},
    )
    with pytest.raises(TypeError, match=method):
        incomplete()
