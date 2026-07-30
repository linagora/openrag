from __future__ import annotations

import pytest


class _FakePool:
    def __init__(self):
        self.query = ""
        self.params: tuple = ()

    async def fetch(self, query: str, *params):
        self.query = query
        self.params = params
        return []


@pytest.mark.asyncio
async def test_candidate_search_escapes_like_wildcards_and_uses_a_cursor():
    from services.persistence.partition_membership_repo import PgPartitionMembershipRepository

    pool = _FakePool()
    repo = PgPartitionMembershipRepository(pool_getter=lambda: pool)

    await repo.list_partition_member_candidates(
        "legal",
        search_prefix=r"Sam_%\\",
        search_user_id=None,
        after_id=42,
        limit=26,
    )

    assert pool.params == ("legal", None, r"Sam\_\%\\\\", 42, 26)
    assert "u.id > $4" in pool.query
    assert "OFFSET" not in pool.query
