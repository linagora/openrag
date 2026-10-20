"""Last-owner guard on partition member removal and role changes (#1182)."""

from __future__ import annotations

import pytest
from core.models.user import PartitionRole
from core.utils.exceptions import ConflictError
from services.persistence.partition_membership_repo import PgPartitionMembershipRepository


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, owners: list[int], member_exists: bool = True):
        self.owners = owners
        self.member_exists = member_exists
        self.fetched: list[str] = []
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetch(self, query: str, *params):
        self.fetched.append(query)
        return [{"user_id": user_id} for user_id in self.owners]

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        verb = query.split()[0]
        return f"{verb} {1 if self.member_exists else 0}"


class _FakePool:
    def __init__(self, owners: list[int], member_exists: bool = True):
        self.conn = _FakeConn(owners, member_exists)

    def acquire(self):
        return _AsyncContext(self.conn)


def _repo(pool: _FakePool) -> PgPartitionMembershipRepository:
    return PgPartitionMembershipRepository(pool_getter=lambda: pool)


@pytest.mark.asyncio
async def test_remove_last_owner_is_refused_without_writing():
    pool = _FakePool(owners=[1])

    assert await _repo(pool).remove_partition_member("default", 1) == "last_owner"

    assert pool.conn.executed == []
    # The owner rows are locked so two concurrent removals cannot both pass.
    assert "FOR UPDATE" in pool.conn.fetched[0]


@pytest.mark.asyncio
async def test_remove_owner_with_another_owner_left_succeeds():
    pool = _FakePool(owners=[1, 2])

    assert await _repo(pool).remove_partition_member("p", 1) == "ok"

    assert any(query.lstrip().startswith("DELETE") for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_remove_non_owner_member_succeeds_even_with_one_owner():
    pool = _FakePool(owners=[1])

    assert await _repo(pool).remove_partition_member("p", 5) == "ok"


@pytest.mark.asyncio
async def test_remove_vanished_member_reports_not_member():
    pool = _FakePool(owners=[1], member_exists=False)

    assert await _repo(pool).remove_partition_member("p", 5) == "not_member"


@pytest.mark.asyncio
async def test_demote_last_owner_is_refused_without_writing():
    pool = _FakePool(owners=[1])

    assert await _repo(pool).update_partition_member_role("default", 1, "editor") == "last_owner"

    assert pool.conn.executed == []
    assert "FOR UPDATE" in pool.conn.fetched[0]


@pytest.mark.asyncio
async def test_demote_owner_with_another_owner_left_succeeds():
    pool = _FakePool(owners=[1, 2])

    assert await _repo(pool).update_partition_member_role("p", 1, "viewer") == "ok"

    assert any(query.lstrip().startswith("UPDATE") for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_last_owner_keeping_owner_role_is_allowed():
    pool = _FakePool(owners=[1])

    assert await _repo(pool).update_partition_member_role("p", 1, "owner") == "ok"


# The typed port methods are what OIDC group sync writes through. They keep
# their bool return and raise the 409 instead, so the sync can skip the change.


@pytest.mark.asyncio
async def test_port_remove_of_last_owner_raises_without_writing():
    pool = _FakePool(owners=[1])

    with pytest.raises(ConflictError) as ei:
        await _repo(pool).remove_partition(1, "alpha")

    assert ei.value.code == "LAST_PARTITION_OWNER"
    assert pool.conn.executed == []
    assert "FOR UPDATE" in pool.conn.fetched[0]


@pytest.mark.asyncio
async def test_port_remove_of_owner_with_another_owner_left_succeeds():
    pool = _FakePool(owners=[1, 2])

    assert await _repo(pool).remove_partition(1, "alpha") is True


@pytest.mark.asyncio
async def test_port_demote_of_last_owner_raises_without_writing():
    pool = _FakePool(owners=[1])

    with pytest.raises(ConflictError) as ei:
        await _repo(pool).update_partition_role(1, "alpha", PartitionRole.VIEWER)

    assert ei.value.code == "LAST_PARTITION_OWNER"
    assert pool.conn.executed == []
    assert "FOR UPDATE" in pool.conn.fetched[0]


@pytest.mark.asyncio
async def test_port_demote_of_owner_with_another_owner_left_succeeds():
    pool = _FakePool(owners=[1, 2])

    assert await _repo(pool).update_partition_role(1, "alpha", PartitionRole.EDITOR) is True


@pytest.mark.asyncio
async def test_port_promote_to_owner_is_allowed():
    pool = _FakePool(owners=[1])

    assert await _repo(pool).update_partition_role(1, "alpha", PartitionRole.OWNER) is True
