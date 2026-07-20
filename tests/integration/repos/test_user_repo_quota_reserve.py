"""Issue #664 — atomic quota reserve/release against a real Postgres.

The point of the reserve is that admission is a *single* conditional
UPDATE, so N concurrent admits at quota can never overshoot. That
property is only meaningful against a real database, hence this suite
rather than a unit test with a fake pool.
"""

from __future__ import annotations

import asyncio

import pytest
from core.models.user import User
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _user(**overrides) -> User:
    defaults = {"display_name": "Quota User", "is_admin": False}
    defaults.update(overrides)
    return User(**defaults)


async def _file_count(store: PostgresStore, user_id: int) -> int:
    user = await store.user_repo.get_user(user_id)
    assert user is not None
    return user.file_count


class TestReserveSemantics:
    async def test_reserve_under_quota_increments(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=3))
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) == 1
        assert await _file_count(postgres_store, user.id) == 1

    async def test_reserve_at_quota_rejects_and_does_not_increment(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=1))
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) == 1
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) is None
        assert await _file_count(postgres_store, user.id) == 1

    async def test_zero_quota_rejects_immediately(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=0))
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=10) is None
        assert await _file_count(postgres_store, user.id) == 0

    async def test_null_quota_falls_back_to_default(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=None))
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=1) == 1
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=1) is None

    async def test_null_quota_with_negative_default_is_unlimited(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=None))
        for expected in (1, 2, 3):
            assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) == expected

    async def test_negative_per_user_quota_is_unlimited(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=-1))
        for expected in (1, 2, 3):
            assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=0) == expected

    async def test_explicit_per_user_quota_honored_when_default_is_negative(self, postgres_store: PostgresStore):
        """A negative *global default* must not make a capped user unlimited."""
        user = await postgres_store.user_repo.create_user(_user(file_quota=2))
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) == 1
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) == 2
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) is None

    async def test_admin_bypasses_quota(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(is_admin=True, file_quota=0))
        # Admins still get counted (file_count stays a truthful total) but are
        # never rejected.
        for expected in (1, 2, 3):
            assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=0) == expected

    async def test_unknown_user_returns_none(self, postgres_store: PostgresStore):
        assert await postgres_store.user_repo.try_reserve_file_slot(99999, default_quota=-1) is None


class TestRelease:
    async def test_release_decrements(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=5))
        await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1)
        await postgres_store.user_repo.release_file_slot(user.id)
        assert await _file_count(postgres_store, user.id) == 0

    async def test_release_clamps_at_zero(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=5))
        await postgres_store.user_repo.release_file_slot(user.id)
        await postgres_store.user_repo.release_file_slot(user.id)
        assert await _file_count(postgres_store, user.id) == 0

    async def test_release_reopens_the_gate(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=1))
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) == 1
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) is None
        await postgres_store.user_repo.release_file_slot(user.id)
        assert await postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) == 1

    async def test_release_on_unknown_user_is_a_noop(self, postgres_store: PostgresStore):
        await postgres_store.user_repo.release_file_slot(99999)


class TestConcurrency:
    @pytest.mark.parametrize("quota", [1, 5])
    async def test_parallel_admits_never_overshoot(self, postgres_store: PostgresStore, quota: int):
        """The regression this issue is about: burst admission at quota.

        20 concurrent reserves against a quota of ``quota`` must grant
        exactly ``quota`` slots — never more — and leave ``file_count``
        exactly at the quota.
        """
        concurrency = 20
        user = await postgres_store.user_repo.create_user(_user(file_quota=quota))

        results = await asyncio.gather(
            *(postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) for _ in range(concurrency))
        )

        granted = [r for r in results if r is not None]
        assert len(granted) == quota, f"expected exactly {quota} admits, got {len(granted)}: {results}"
        # Every granted reservation reports a distinct, contiguous count.
        assert sorted(granted) == list(range(1, quota + 1))
        assert await _file_count(postgres_store, user.id) == quota

    async def test_parallel_reserve_and_release_settles(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user(file_quota=10))
        await asyncio.gather(
            *(postgres_store.user_repo.try_reserve_file_slot(user.id, default_quota=-1) for _ in range(10))
        )
        await asyncio.gather(*(postgres_store.user_repo.release_file_slot(user.id) for _ in range(10)))
        assert await _file_count(postgres_store, user.id) == 0
