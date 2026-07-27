"""Postgres implementation of :class:`PartitionMembershipRepository`.

Backs the ``partition_memberships`` table. Split out of
:class:`~services.persistence.user_repo.PgUserRepository` so the catalog
matches the 7A.2 one-repo-per-entity layout.

Two parallel APIs live here on the same table:

* The clean port methods (:meth:`assign_partition`, :meth:`list_user_partitions`,
  …) typed with the :class:`~core.models.user.UserPartition` /
  :class:`~core.models.user.PartitionRole` domain models.
* The legacy ``*_partition_member`` / ``*_dict`` methods consumed by the
  Phase 7C shim (``vectordb_shims.py``). These return plain dicts to match
  the old ``PartitionFileManager`` signatures and are removed in Phase 9
  once the shim is deleted (each carries a ``TODO(phase-9)``).

``PgUserRepository`` still reads this table directly (``_fetch_memberships``)
to hydrate the ``User`` aggregate's ``partitions`` field — that is a
read-only denormalisation inside the user aggregate boundary, not membership
management, so it stays there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from core.models.user import PartitionRole, UserPartition
from core.ports.partition_membership_repo import PartitionMembershipRepository

if TYPE_CHECKING:
    import asyncpg


class PgPartitionMembershipRepository(PartitionMembershipRepository):
    """asyncpg-backed implementation of :class:`PartitionMembershipRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ── Partition memberships ────────────────────────────────────────

    async def assign_partition(self, assignment: UserPartition) -> UserPartition:
        """Idempotent upsert of (partition, user_id) → role.

        Returns the row as actually persisted (re-reads the DB so the
        timestamp reflects what's on disk).
        """
        await self.pool.execute(
            """
            INSERT INTO partition_memberships (partition_name, user_id, role, added_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (partition_name, user_id)
              DO UPDATE SET role = EXCLUDED.role
            """,
            assignment.partition,
            assignment.user_id,
            assignment.role.value,
        )
        row = await self.pool.fetchrow(
            """
            SELECT * FROM partition_memberships
            WHERE partition_name = $1 AND user_id = $2
            """,
            assignment.partition,
            assignment.user_id,
        )
        return self._row_to_user_partition(row)

    async def remove_partition(self, user_id: int, partition: str) -> bool:
        result = await self.pool.execute(
            """
            DELETE FROM partition_memberships
            WHERE user_id = $1 AND partition_name = $2
            """,
            user_id,
            partition,
        )
        return result.endswith(" 1")

    async def list_user_partitions(self, user_id: int) -> list[UserPartition]:
        rows = await self.pool.fetch(
            "SELECT * FROM partition_memberships WHERE user_id = $1 ORDER BY added_at",
            user_id,
        )
        return [self._row_to_user_partition(r) for r in rows]

    async def list_partition_users(self, partition: str) -> list[UserPartition]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM partition_memberships
            WHERE partition_name = $1
            ORDER BY added_at
            """,
            partition,
        )
        return [self._row_to_user_partition(r) for r in rows]

    async def update_partition_role(
        self,
        user_id: int,
        partition: str,
        role: PartitionRole,
    ) -> bool:
        result = await self.pool.execute(
            """
            UPDATE partition_memberships SET role = $3
            WHERE user_id = $1 AND partition_name = $2
            """,
            user_id,
            partition,
            role.value,
        )
        return result.endswith(" 1")

    async def count_partition_users(self, partition: str) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*)::int FROM partition_memberships WHERE partition_name = $1",
            partition,
        )

    async def list_partition_member_candidates(
        self,
        partition: str,
        *,
        search: str | None,
        offset: int,
        limit: int,
    ) -> list[dict]:
        """Return a bounded page of users who are not partition members."""
        rows = await self.pool.fetch(
            """
            SELECT u.id AS user_id, u.display_name
            FROM users u
            WHERE NOT EXISTS (
                SELECT 1
                FROM partition_memberships m
                WHERE m.partition_name = $1
                  AND m.user_id = u.id
            )
              AND (
                $2::text IS NULL
                OR STRPOS(LOWER(COALESCE(u.display_name, '')), LOWER($2)) > 0
                OR STRPOS(u.id::text, $2) > 0
              )
            ORDER BY u.id
            LIMIT $3 OFFSET $4
            """,
            partition,
            search,
            limit,
            offset,
        )
        return [
            {
                "user_id": row["user_id"],
                "display_name": row["display_name"],
            }
            for row in rows
        ]

    # ── Legacy method names used by the Phase 7C shim ────────────────

    async def list_partition_members(self, partition: str) -> list[dict]:
        """TODO(phase-9): remove. Returns empty list when the partition does not exist."""
        exists = await self.pool.fetchval(
            "SELECT 1 FROM partitions WHERE partition = $1",
            partition,
        )
        if not exists:
            return []
        rows = await self.pool.fetch(
            """
            SELECT * FROM partition_memberships
            WHERE partition_name = $1
            ORDER BY added_at
            """,
            partition,
        )
        return [
            {
                "user_id": r["user_id"],
                "role": r["role"],
                "added_at": r["added_at"].isoformat() if r["added_at"] else None,
            }
            for r in rows
        ]

    async def add_partition_member(self, partition: str, user_id: int, role: str) -> bool:
        """TODO(phase-9): remove. Creates the partition row on first use."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM partitions WHERE partition = $1",
                    partition,
                )
                if not exists and role != "owner":
                    raise ValueError("The first member must have role='owner'")
                await conn.execute(
                    """
                    INSERT INTO partitions (partition, created_at)
                    VALUES ($1, NOW())
                    ON CONFLICT (partition) DO NOTHING
                    """,
                    partition,
                )
                await conn.execute(
                    """
                    INSERT INTO partition_memberships
                        (partition_name, user_id, role, added_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (partition_name, user_id)
                      DO UPDATE SET role = EXCLUDED.role
                    """,
                    partition,
                    user_id,
                    role,
                )
        return True

    async def remove_partition_member(self, partition: str, user_id: int) -> bool:
        """TODO(phase-9): remove."""
        result = await self.pool.execute(
            """
            DELETE FROM partition_memberships
            WHERE partition_name = $1 AND user_id = $2
            """,
            partition,
            user_id,
        )
        return result.endswith(" 1")

    async def update_partition_member_role(
        self,
        partition: str,
        user_id: int,
        new_role: str,
    ) -> bool:
        """TODO(phase-9): remove."""
        result = await self.pool.execute(
            """
            UPDATE partition_memberships SET role = $3
            WHERE partition_name = $1 AND user_id = $2
            """,
            partition,
            user_id,
            new_role,
        )
        return result.endswith(" 1")

    async def user_is_partition_member(self, user_id: int, partition: str) -> bool:
        """TODO(phase-9): remove."""
        return await self.pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM partition_memberships
                WHERE user_id = $1 AND partition_name = $2
            )
            """,
            user_id,
            partition,
        )

    async def list_user_partitions_dict(self, user_id: int) -> list[dict]:
        """TODO(phase-9): remove. Legacy ``Partition.to_dict()``-style rows."""
        rows = await self.pool.fetch(
            """
            SELECT p.partition, p.created_at, m.role
            FROM partitions p
            JOIN partition_memberships m
              ON m.partition_name = p.partition
            WHERE m.user_id = $1
            """,
            user_id,
        )
        return [
            {
                "partition": r["partition"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "role": r["role"],
            }
            for r in rows
        ]

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_user_partition(row: asyncpg.Record) -> UserPartition:
        return UserPartition(
            user_id=row["user_id"],
            partition=row["partition_name"],
            role=PartitionRole(row["role"]),
            added_at=row["added_at"],
        )


__all__ = ["PgPartitionMembershipRepository"]
