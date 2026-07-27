"""Phase 7A.2 — PgPartitionMembershipRepository against a real Postgres.

Split out of ``test_user_repo.py`` when partition memberships moved off
``PgUserRepository`` into their own repo (one-repo-per-entity, 7A.2).
"""

from __future__ import annotations

import pytest
from core.models.user import PartitionRole, User, UserPartition
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _user(**overrides) -> User:
    defaults = {
        "display_name": "Alice",
        "email": "alice@example.com",
        "is_admin": False,
    }
    defaults.update(overrides)
    return User(**defaults)


class TestPartitionMemberships:
    async def test_assign_then_list(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user())
        await postgres_store.partition_repo.create_partition("docs")
        await postgres_store.membership_repo.assign_partition(
            UserPartition(user_id=user.id, partition="docs", role=PartitionRole.OWNER),
        )
        memberships = await postgres_store.membership_repo.list_user_partitions(user.id)
        assert len(memberships) == 1
        assert memberships[0].partition == "docs"
        assert memberships[0].role == PartitionRole.OWNER

    async def test_assign_is_idempotent_and_updates_role(
        self,
        postgres_store: PostgresStore,
    ):
        user = await postgres_store.user_repo.create_user(_user())
        await postgres_store.partition_repo.create_partition("docs")
        await postgres_store.membership_repo.assign_partition(
            UserPartition(user_id=user.id, partition="docs", role=PartitionRole.VIEWER),
        )
        await postgres_store.membership_repo.assign_partition(
            UserPartition(user_id=user.id, partition="docs", role=PartitionRole.OWNER),
        )
        memberships = await postgres_store.membership_repo.list_user_partitions(user.id)
        assert len(memberships) == 1
        assert memberships[0].role == PartitionRole.OWNER

    async def test_remove_partition(self, postgres_store: PostgresStore):
        user = await postgres_store.user_repo.create_user(_user())
        await postgres_store.partition_repo.create_partition("docs")
        await postgres_store.membership_repo.assign_partition(
            UserPartition(user_id=user.id, partition="docs"),
        )
        assert await postgres_store.membership_repo.remove_partition(user.id, "docs") is True
        assert await postgres_store.membership_repo.list_user_partitions(user.id) == []

    async def test_candidate_search_is_paginated_and_excludes_members(
        self,
        postgres_store: PostgresStore,
    ):
        member = await postgres_store.user_repo.create_user(
            _user(display_name="Sam Lee", email="member@example.com"),
        )
        candidate = await postgres_store.user_repo.create_user(
            _user(display_name="Sam Lee", email="candidate@example.com"),
        )
        third = await postgres_store.user_repo.create_user(
            _user(display_name="Taylor", email="taylor@example.com"),
        )
        fourth = await postgres_store.user_repo.create_user(
            _user(display_name="Jordan", email="jordan@example.com"),
        )
        await postgres_store.partition_repo.create_partition("docs")
        await postgres_store.membership_repo.assign_partition(
            UserPartition(user_id=member.id, partition="docs"),
        )

        matches = await postgres_store.membership_repo.list_partition_member_candidates(
            "docs",
            search="SAM",
            offset=0,
            limit=10,
        )
        assert matches == [
            {
                "user_id": candidate.id,
                "display_name": "Sam Lee",
            }
        ]

        first_page = await postgres_store.membership_repo.list_partition_member_candidates(
            "docs",
            search=None,
            offset=0,
            limit=2,
        )
        second_page = await postgres_store.membership_repo.list_partition_member_candidates(
            "docs",
            search=str(fourth.id),
            offset=0,
            limit=2,
        )
        assert [row["user_id"] for row in first_page] == [candidate.id, third.id]
        assert [row["user_id"] for row in second_page] == [fourth.id]
