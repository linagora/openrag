import pytest


class _FakePool:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self._fetch_result: list = []
        self._fetchval_result: int = 0

    async def fetch(self, query: str, *params):
        self.executed.append((query, params))
        return self._fetch_result

    async def fetchval(self, query: str, *params):
        self.executed.append((query, params))
        return self._fetchval_result

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "DELETE 2"


def _row(**kwargs):
    base = {
        "document_id": "file-1",
        "partition": "tenant-a",
        "tag": "finance",
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_bulk_insert_normalizes_and_deduplicates_tags():
    from services.persistence.topic_tag_repo import PgTopicTagRepository

    pool = _FakePool()
    pool._fetchval_result = 2
    repo = PgTopicTagRepository(pool_getter=lambda: pool)

    inserted = await repo.bulk_insert(
        [
            {"document_id": "file-1", "partition": "tenant-a", "tag": "Finance"},
            {"document_id": "file-1", "partition": "tenant-a", "tag": " finance "},
            {"document_id": "file-1", "partition": "tenant-a", "tag": "risk"},
        ]
    )

    assert inserted == 2
    query, params = pool.executed[0]
    assert "INSERT INTO topic_tags" in query
    assert params[0] == ["file-1", "file-1"]
    assert params[1] == ["tenant-a", "tenant-a"]
    assert params[2] == ["Finance", "risk"]
    assert params[3] == ["finance", "risk"]


@pytest.mark.asyncio
async def test_get_by_document_returns_rows_ordered_by_tag():
    from services.persistence.topic_tag_repo import PgTopicTagRepository

    pool = _FakePool()
    pool._fetch_result = [_row(tag="finance"), _row(tag="risk")]
    repo = PgTopicTagRepository(pool_getter=lambda: pool)

    result = await repo.get_by_document("file-1", partition="tenant-a")

    assert [row["tag"] for row in result] == ["finance", "risk"]
    query, params = pool.executed[0]
    assert "WHERE document_id = $1 AND partition = $2" in query
    assert "ORDER BY normalized_tag" in query
    assert params == ("file-1", "tenant-a")


@pytest.mark.asyncio
async def test_delete_by_document_returns_affected_count():
    from services.persistence.topic_tag_repo import PgTopicTagRepository

    pool = _FakePool()
    repo = PgTopicTagRepository(pool_getter=lambda: pool)

    count = await repo.delete_by_document("file-1", partition="tenant-a")

    assert count == 2
    query, params = pool.executed[0]
    assert "DELETE FROM topic_tags" in query
    assert "WHERE document_id = $1 AND partition = $2" in query
    assert params == ("file-1", "tenant-a")


@pytest.mark.asyncio
async def test_search_is_partition_scoped_and_case_insensitive():
    from services.persistence.topic_tag_repo import PgTopicTagRepository

    pool = _FakePool()
    pool._fetch_result = [_row()]
    repo = PgTopicTagRepository(pool_getter=lambda: pool)

    await repo.search("tenant-a", "Finance", top_k=3)

    query, params = pool.executed[0]
    assert "WHERE partition = $1 AND normalized_tag = $2" in query
    assert "LIMIT $3" in query
    assert params == ("tenant-a", "finance", 3)
