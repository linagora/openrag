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
