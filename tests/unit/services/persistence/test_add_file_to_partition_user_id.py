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
    def __init__(self, *, partition_exists: bool = False):
        self.executed: list[tuple[str, tuple]] = []
        self.partition_exists = partition_exists

    def transaction(self):
        return _AsyncContext(self)

    async def fetchval(self, query: str, *params):
        if "SELECT 1 FROM files" in query:
            return None
        if "SELECT 1 FROM partitions" in query:
            return 1 if self.partition_exists else None
        if "RETURNING 1" in query:
            return 1
        return None

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, *, partition_exists: bool = False):
        self.conn = _FakeConn(partition_exists=partition_exists)

    def acquire(self):
        return _AsyncContext(self.conn)


class _DirectExecutePool:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "UPDATE 1"


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


@pytest.mark.asyncio
async def test_add_file_to_partition_persists_indexation_config_column():
    from services.persistence.document_repo import PgDocumentRepository

    pool = _FakePool()
    repo = PgDocumentRepository(pool_getter=lambda: pool)
    snapshot = {"parsing_strategy": "pymupdf"}

    assert (
        await repo.add_file_to_partition(
            file_id="f1",
            partition="new-part",
            user_id=42,
            indexation_config=snapshot,
        )
        is True
    )
    insert_query, insert_params = next(
        (query, params) for query, params in pool.conn.executed if "INSERT INTO files" in query
    )
    assert "indexation_config" in insert_query
    assert snapshot in insert_params


@pytest.mark.asyncio
async def test_add_file_to_partition_persists_content_hash_column():
    from services.persistence.document_repo import PgDocumentRepository

    pool = _FakePool()
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    assert (
        await repo.add_file_to_partition(
            file_id="f1",
            partition="new-part",
            user_id=42,
            content_sha256="abc123",
        )
        is True
    )
    insert_query, insert_params = next(
        (query, params) for query, params in pool.conn.executed if "INSERT INTO files" in query
    )
    assert "content_sha256" in insert_query
    assert "abc123" in insert_params


@pytest.mark.asyncio
async def test_require_existing_partition_does_not_auto_create_missing_partition():
    from services.persistence.document_repo import PgDocumentRepository

    pool = _FakePool(partition_exists=False)
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    assert (
        await repo.add_file_to_partition(
            file_id="f1",
            partition="deleted-part",
            user_id=42,
            require_existing_partition=True,
        )
        is False
    )
    assert not any("INSERT INTO partitions" in query for query, _ in pool.conn.executed)
    assert not any("INSERT INTO files" in query for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_require_existing_partition_inserts_when_partition_exists():
    from services.persistence.document_repo import PgDocumentRepository

    pool = _FakePool(partition_exists=True)
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    assert (
        await repo.add_file_to_partition(
            file_id="f1",
            partition="tenant-a",
            user_id=42,
            require_existing_partition=True,
        )
        is True
    )
    assert not any("INSERT INTO partitions" in query for query, _ in pool.conn.executed)
    assert any("INSERT INTO files" in query for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_update_file_in_partition_persists_indexation_config_column():
    from services.persistence.document_repo import PgDocumentRepository

    pool = _DirectExecutePool()
    repo = PgDocumentRepository(pool_getter=lambda: pool)
    snapshot = {"parsing_strategy": "marker"}

    assert await repo.update_file_in_partition("f1", "tenant-a", indexation_config=snapshot) is True

    query, params = pool.executed[0]
    assert "indexation_config" in query
    assert snapshot in params


@pytest.mark.asyncio
async def test_update_file_in_partition_updates_content_hash_column():
    from services.persistence.document_repo import PgDocumentRepository

    pool = _DirectExecutePool()
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    assert await repo.update_file_in_partition("f1", "tenant-a", content_sha256="abc123") is True

    query, params = pool.executed[0]
    assert "content_sha256" in query
    assert "abc123" in params


def test_row_to_document_exposes_indexation_config_snapshot():
    from services.persistence.document_repo import PgDocumentRepository

    snapshot = {"parsing_strategy": "marker"}
    row = {
        "file_id": "f1",
        "partition_name": "tenant-a",
        "file_metadata": {"filename": "doc.txt"},
        "created_by": 42,
        "relationship_id": None,
        "parent_id": None,
        "indexation_config": snapshot,
    }

    doc = PgDocumentRepository._row_to_document(row)

    assert doc.indexation_config == snapshot
