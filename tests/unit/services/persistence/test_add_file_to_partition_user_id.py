from __future__ import annotations

import pytest


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetchval(self, query: str, *params):
        if "SELECT 1 FROM files" in query:
            return None
        if "RETURNING 1" in query:
            return 1
        return None

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _AsyncContext(self.conn)


@pytest.mark.asyncio
async def test_auto_create_refuses_when_user_id_is_none():
    from services.persistence.document_repo import PgDocumentRepository

    pool = _FakePool()
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    with pytest.raises(ValueError, match="without a user_id"):
        await repo.add_file_to_partition(file_id="f1", partition="new-part", user_id=None)

    assert not any("INSERT INTO files" in query for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_auto_create_succeeds_with_real_user_id():
    from services.persistence.document_repo import PgDocumentRepository

    pool = _FakePool()
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    assert await repo.add_file_to_partition(file_id="f1", partition="new-part", user_id=42) is True
    assert any("INSERT INTO files" in query for query, _ in pool.conn.executed)
