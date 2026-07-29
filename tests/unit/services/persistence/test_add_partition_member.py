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
    def __init__(self, partition_exists: bool, membership_created: bool = True):
        self.partition_exists = partition_exists
        self.membership_created = membership_created
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetchval(self, query: str, *params):
        if "SELECT 1 FROM partitions" in query:
            return 1 if self.partition_exists else None
        if "INSERT INTO partition_memberships" in query:
            self.executed.append((query, params))
            return 1 if self.membership_created else None
        return None

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, partition_exists: bool, membership_created: bool = True):
        self.conn = _FakeConn(partition_exists, membership_created)

    def acquire(self):
        return _AsyncContext(self.conn)


@pytest.mark.asyncio
async def test_add_member_with_editor_role_refuses_to_create_partition():
    from services.persistence.partition_membership_repo import PgPartitionMembershipRepository

    pool = _FakePool(partition_exists=False)
    repo = PgPartitionMembershipRepository(pool_getter=lambda: pool)

    with pytest.raises(ValueError, match="first member must have role='owner'"):
        await repo.add_partition_member("brand-new", 5, "editor")

    assert pool.conn.executed == []


@pytest.mark.asyncio
async def test_add_member_with_owner_role_creates_partition():
    from services.persistence.partition_membership_repo import PgPartitionMembershipRepository

    pool = _FakePool(partition_exists=False)
    repo = PgPartitionMembershipRepository(pool_getter=lambda: pool)

    assert await repo.add_partition_member("brand-new", 5, "owner") is True
    assert any("INSERT INTO partitions" in query for query, _ in pool.conn.executed)
    assert any("INSERT INTO partition_memberships" in query for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_add_member_to_existing_partition_allows_any_role():
    from services.persistence.partition_membership_repo import PgPartitionMembershipRepository

    pool = _FakePool(partition_exists=True)
    repo = PgPartitionMembershipRepository(pool_getter=lambda: pool)

    assert await repo.add_partition_member("existing", 6, "editor") is True
    assert any("INSERT INTO partition_memberships" in query for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_add_member_conflict_does_not_overwrite_existing_role():
    from services.persistence.partition_membership_repo import PgPartitionMembershipRepository

    pool = _FakePool(partition_exists=True, membership_created=False)
    repo = PgPartitionMembershipRepository(pool_getter=lambda: pool)

    assert await repo.add_partition_member("existing", 6, "editor") is False
    membership_query = next(query for query, _ in pool.conn.executed if "INSERT INTO partition_memberships" in query)
    assert "DO NOTHING" in membership_query
    assert "DO UPDATE" not in membership_query
    assert "RETURNING 1" in membership_query
