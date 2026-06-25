from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.utils.exceptions import ValidationError


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, *, owned_count: int = 0):
        self.owned_count = owned_count
        self.operations: list[tuple[str, tuple]] = []
        self.inserted_partition = False

    def transaction(self):
        self.operations.append(("BEGIN", ()))
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *params):
        self.operations.append((query, params))
        if "SELECT * FROM partitions" in query:
            return None
        if "INSERT INTO partitions" in query:
            self.inserted_partition = True
            return {"partition": params[0], "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
        return None

    async def fetchval(self, query: str, *params):
        self.operations.append((query, params))
        if "COUNT(*)::int FROM partition_memberships" in query:
            return self.owned_count
        return None

    async def execute(self, query: str, *params):
        self.operations.append((query, params))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    def acquire(self):
        return _AsyncContext(self.conn)


@pytest.mark.asyncio
async def test_create_partition_enforces_owner_cap_inside_transaction():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn(owned_count=2)
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    with pytest.raises(ValidationError) as exc:
        await repo.create_partition("new", user_id=7, max_owned=2)

    assert exc.value.code == "PARTITION_LIMIT_EXCEEDED"
    assert conn.inserted_partition is False
    operations = [query for query, _params in conn.operations]
    assert any("pg_advisory_xact_lock" in query for query in operations)
    assert any("COUNT(*)::int FROM partition_memberships" in query for query in operations)
    assert not any("INSERT INTO partitions" in query for query in operations)


@pytest.mark.asyncio
async def test_create_partition_locks_user_before_counting_owned_partitions():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn(owned_count=1)
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    await repo.create_partition("new", user_id=7, max_owned=2)

    operations = [query for query, _params in conn.operations]
    lock_index = next(i for i, query in enumerate(operations) if "pg_advisory_xact_lock" in query)
    count_index = next(i for i, query in enumerate(operations) if "COUNT(*)::int FROM partition_memberships" in query)
    insert_index = next(i for i, query in enumerate(operations) if "INSERT INTO partitions" in query)
    assert lock_index < count_index < insert_index
