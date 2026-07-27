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
    def __init__(self, *, owned_count: int = 0, existing_partition: bool = False):
        self.owned_count = owned_count
        self.existing_partition = existing_partition
        self.operations: list[tuple[str, tuple]] = []
        self.inserted_partition = False

    def transaction(self):
        self.operations.append(("BEGIN", ()))
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *params):
        self.operations.append((query, params))
        if "SELECT * FROM partitions" in query:
            if self.existing_partition:
                return {"partition": params[0], "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
            return None
        if "INSERT INTO partitions" in query:
            self.inserted_partition = True
            return {"partition": params[0], "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
        return None

    async def fetchval(self, query: str, *params):
        self.operations.append((query, params))
        if "SELECT EXISTS (SELECT 1 FROM partitions" in query:
            return self.existing_partition
        if "COUNT(*)::int FROM partition_memberships" in query:
            return self.owned_count
        return None

    async def execute(self, query: str, *params):
        self.operations.append((query, params))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1
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


@pytest.mark.asyncio
async def test_create_partition_existing_row_raises_conflict():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn(existing_partition=True)
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    with pytest.raises(ValidationError) as exc:
        await repo.create_partition("existing", user_id=7, max_owned=2)

    assert exc.value.status_code == 409
    assert exc.value.code == "PARTITION_EXISTS"
    assert conn.inserted_partition is False


@pytest.mark.asyncio
async def test_partition_operation_lock_uses_session_advisory_lock_and_unlocks_on_error():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn()
    pool = _FakePool(conn)
    repo = PgPartitionRepository(pool_getter=lambda: pool)

    with pytest.raises(RuntimeError, match="inside fence"):
        async with repo.partition_operation_lock("tenant-a"):
            raise RuntimeError("inside fence")

    operations = [(query, params) for query, params in conn.operations if "pg_advisory_" in query]
    assert [query for query, _ in operations] == [
        "SELECT pg_advisory_lock($1::integer, hashtext($2)::integer)",
        "SELECT pg_advisory_unlock($1::integer, hashtext($2)::integer)",
    ]
    assert operations[0][1] == operations[1][1]
    assert operations[0][1][1] == "tenant-a"
    assert pool.acquire_count == 1


@pytest.mark.asyncio
async def test_partition_operation_guard_checks_existence_on_lock_connection():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn(existing_partition=True)
    pool = _FakePool(conn)
    repo = PgPartitionRepository(pool_getter=lambda: pool)

    async with repo.partition_operation_lock("tenant-a") as operation:
        assert await operation.partition_exists("tenant-a") is True

    operations = [query for query, _params in conn.operations]
    assert operations == [
        "SELECT pg_advisory_lock($1::integer, hashtext($2)::integer)",
        "SELECT EXISTS (SELECT 1 FROM partitions WHERE partition = $1)",
        "SELECT pg_advisory_unlock($1::integer, hashtext($2)::integer)",
    ]
    assert pool.acquire_count == 1


# ── update_partition preset-assignment race guard ────────────────────


def _full_partition_row(**overrides):
    row = {
        "partition": "p1",
        "description": None,
        "embedder": "default",
        "indexation_preset": "legal",
        "retrieval_preset": "default",
        "dimension": None,
        "collection_name": None,
        "chat_history_depth": 0,
        "chat_llm": None,
        "generation_prompt_names": {},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    row.update(overrides)
    return row


class _UpdateFakeConn:
    """Conn/pool double for exercising update_partition's transactional guard.

    ``preset_exists`` models whether the referenced preset row is still present
    when the guard's follow-up SELECT runs (False simulates a concurrent
    delete_preset committing while this UPDATE was blocked on its SHARE lock).
    """

    def __init__(self, *, preset_exists: bool = True):
        self.preset_exists = preset_exists
        self.operations: list[tuple[str, tuple]] = []
        self.transactions = 0

    # pool interface
    def acquire(self):
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *params):
        self.operations.append((query, params))
        if "UPDATE partitions" in query:
            # Reflect the assigned columns back so the returned row matches the
            # write. ``$n`` placeholders are 1-indexed into params ($1 = name).
            body = query.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            update = {}
            for frag in body.split(", "):
                col, _, ref = frag.strip().partition(" = $")
                if ref.isdigit():
                    update[col.strip()] = params[int(ref) - 1]
            return _full_partition_row(**update)
        return None

    async def fetchval(self, query: str, *params):
        self.operations.append((query, params))
        if "FROM pipeline_presets" in query:
            return 1 if self.preset_exists else None
        return None

    # conn interface
    def transaction(self):
        self.transactions += 1
        self.operations.append(("BEGIN", ()))
        return _AsyncContext(self)


@pytest.mark.asyncio
async def test_update_partition_rolls_back_when_preset_deleted_concurrently():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(preset_exists=False)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    with pytest.raises(ValidationError) as exc:
        await repo.update_partition("p1", indexation_preset="just-deleted")

    assert exc.value.code == "PRESET_NOT_FOUND"
    # The write and the existence check share one transaction, and the write
    # (partitions) happens before the check (pipeline_presets) — the lock order
    # that keeps it deadlock-free against delete_preset. Raising inside the
    # transaction rolls the UPDATE back.
    assert conn.transactions == 1
    queries = [q for q, _ in conn.operations]
    update_i = next(i for i, q in enumerate(queries) if "UPDATE partitions" in q)
    check_i = next(i for i, q in enumerate(queries) if "FROM pipeline_presets" in q)
    assert update_i < check_i


@pytest.mark.asyncio
async def test_update_partition_commits_when_preset_exists():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(preset_exists=True)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    result = await repo.update_partition("p1", indexation_preset="legal")

    assert result["indexation_preset"] == "legal"
    assert conn.transactions == 1
    assert any("FROM pipeline_presets" in q for q, _ in conn.operations)


@pytest.mark.asyncio
async def test_update_partition_without_preset_change_skips_the_guard():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(preset_exists=False)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    result = await repo.update_partition("p1", description="notes")

    # Non-preset updates take the fast path: no transaction, no preset lookup.
    assert result["description"] == "notes"
    assert conn.transactions == 0
    assert not any("FROM pipeline_presets" in q for q, _ in conn.operations)
