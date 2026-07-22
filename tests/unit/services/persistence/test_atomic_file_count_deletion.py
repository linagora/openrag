from __future__ import annotations

import pytest


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Transaction(_AsyncContext):
    async def __aexit__(self, exc_type, exc, tb):
        self.value.transaction_exits.append(exc_type)
        return False


class _DeleteConn:
    def __init__(
        self,
        *,
        single_row=None,
        deleted_rows=None,
        partition_row=None,
        fail_counter_update: bool = False,
    ):
        self.single_row = single_row
        self.deleted_rows = deleted_rows or []
        self.partition_row = partition_row
        self.fail_counter_update = fail_counter_update
        self.operations: list[tuple[str, tuple]] = []
        self.transaction_exits: list[type[BaseException] | None] = []

    def transaction(self):
        self.operations.append(("BEGIN", ()))
        return _Transaction(self)

    async def fetchrow(self, query: str, *params):
        self.operations.append((query, params))
        if "SELECT partition FROM partitions" in query:
            return self.partition_row
        return self.single_row

    async def fetch(self, query: str, *params):
        self.operations.append((query, params))
        return self.deleted_rows

    async def execute(self, query: str, *params):
        self.operations.append((query, params))
        if self.fail_counter_update and "UPDATE users SET file_count" in query:
            raise RuntimeError("counter update failed")
        return "UPDATE 1"


class _DeletePool:
    def __init__(self, conn: _DeleteConn):
        self.conn = conn

    def acquire(self):
        return _AsyncContext(self.conn)


def _queries(conn: _DeleteConn) -> list[str]:
    return [query for query, _params in conn.operations if query != "BEGIN"]


def _counter_updates(conn: _DeleteConn) -> list[tuple]:
    return [params for query, params in conn.operations if "UPDATE users SET file_count" in query]


@pytest.mark.asyncio
async def test_scoped_delete_accounts_only_for_the_returned_row():
    from services.persistence.document_repo import PgDocumentRepository

    conn = _DeleteConn(single_row={"created_by": 7})
    repo = PgDocumentRepository(pool_getter=lambda: _DeletePool(conn))

    assert await repo.remove_file_from_partition("file-1", "tenant-a") is True
    assert "DELETE FROM files" in _queries(conn)[0]
    assert "RETURNING created_by" in _queries(conn)[0]
    assert not any(query.lstrip().startswith("SELECT") for query in _queries(conn))
    assert _counter_updates(conn) == [(1, 7)]


@pytest.mark.asyncio
async def test_scoped_delete_retry_is_a_noop():
    from services.persistence.document_repo import PgDocumentRepository

    conn = _DeleteConn(single_row=None)
    repo = PgDocumentRepository(pool_getter=lambda: _DeletePool(conn))

    assert await repo.remove_file_from_partition("missing", "tenant-a") is False
    assert _counter_updates(conn) == []


@pytest.mark.asyncio
async def test_unscoped_delete_keeps_one_row_semantics_in_one_statement():
    from services.persistence.document_repo import PgDocumentRepository

    conn = _DeleteConn(single_row={"created_by": None})
    repo = PgDocumentRepository(pool_getter=lambda: _DeletePool(conn))

    assert await repo.delete_document("shared-file-id") is True
    assert len(_queries(conn)) == 1
    assert "WITH target AS" in _queries(conn)[0]
    assert "ORDER BY id" in _queries(conn)[0]
    assert "DELETE FROM files" in _queries(conn)[0]
    assert _counter_updates(conn) == []


@pytest.mark.asyncio
async def test_bulk_delete_uses_returned_rows_and_stable_user_order():
    from services.persistence.document_repo import PgDocumentRepository

    conn = _DeleteConn(
        deleted_rows=[
            {"created_by": 9},
            {"created_by": None},
            {"created_by": 2},
            {"created_by": 9},
        ]
    )
    repo = PgDocumentRepository(pool_getter=lambda: _DeletePool(conn))

    assert await repo.delete_documents_by_partition("tenant-a") == 4
    assert "DELETE FROM files" in _queries(conn)[0]
    assert "RETURNING created_by" in _queries(conn)[0]
    assert not any("GROUP BY created_by" in query for query in _queries(conn))
    assert _counter_updates(conn) == [(1, 2), (2, 9)]


@pytest.mark.asyncio
async def test_partition_delete_locks_then_accounts_for_returned_rows():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _DeleteConn(
        partition_row={"partition": "tenant-a"},
        deleted_rows=[{"created_by": 9}, {"created_by": 2}],
    )
    repo = PgPartitionRepository(pool_getter=lambda: _DeletePool(conn))

    assert await repo.delete_partition("tenant-a") is True
    queries = _queries(conn)
    assert "FOR UPDATE" in queries[0]
    assert "DELETE FROM files" in queries[1]
    assert "RETURNING created_by" in queries[1]
    assert _counter_updates(conn) == [(1, 2), (1, 9)]
    assert queries[-1] == "DELETE FROM partitions WHERE partition = $1"


@pytest.mark.asyncio
async def test_partition_delete_retry_is_a_noop():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _DeleteConn(partition_row=None)
    repo = PgPartitionRepository(pool_getter=lambda: _DeletePool(conn))

    assert await repo.delete_partition("missing") is False
    assert len(_queries(conn)) == 1
    assert _counter_updates(conn) == []


@pytest.mark.asyncio
async def test_counter_failure_escapes_the_delete_transaction():
    from services.persistence.document_repo import PgDocumentRepository

    conn = _DeleteConn(single_row={"created_by": 7}, fail_counter_update=True)
    repo = PgDocumentRepository(pool_getter=lambda: _DeletePool(conn))

    with pytest.raises(RuntimeError, match="counter update failed"):
        await repo.remove_file_from_partition("file-1", "tenant-a")

    assert conn.transaction_exits == [RuntimeError]
