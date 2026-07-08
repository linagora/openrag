"""Partition membership repository interface.

Split out of :class:`~openrag.core.ports.user_repo.UserRepository` so the
catalog matches the 7A.2 one-repo-per-entity layout — ``partition_memberships``
is its own table and gets its own port. ``UserRepository`` still reads
memberships internally to hydrate the ``User`` aggregate's ``partitions``
field, but all membership *management* lives here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from openrag.core.models.user import PartitionRole, UserPartition


class PartitionMembershipRepository(ABC):
    """CRUD operations for partition memberships (owner/editor/viewer)."""

    @abstractmethod
    async def assign_partition(self, assignment: UserPartition) -> UserPartition: ...

    @abstractmethod
    async def remove_partition(self, user_id: int, partition: str) -> bool: ...

    @abstractmethod
    async def list_user_partitions(self, user_id: int) -> list[UserPartition]: ...

    @abstractmethod
    async def list_partition_users(self, partition: str) -> list[UserPartition]: ...

    @abstractmethod
    async def update_partition_role(self, user_id: int, partition: str, role: PartitionRole) -> bool: ...

    @abstractmethod
    async def count_partition_users(self, partition: str) -> int: ...
