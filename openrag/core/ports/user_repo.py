"""User repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openrag.core.models.user import User, UserPartition


class UserRepository(ABC):
    """CRUD operations for users and partition memberships."""

    @abstractmethod
    async def create_user(self, user: User) -> User: ...

    @abstractmethod
    async def get_user(self, user_id: int) -> User | None: ...

    @abstractmethod
    async def get_user_by_token(self, token_hash: str) -> User | None: ...

    @abstractmethod
    async def get_user_by_external_id(self, external_id: str) -> User | None: ...

    @abstractmethod
    async def list_users(self, offset: int = 0, limit: int = 50) -> list[User]: ...

    @abstractmethod
    async def update_user(self, user_id: int, **fields: Any) -> User | None: ...

    @abstractmethod
    async def delete_user(self, user_id: int) -> bool: ...

    # ── Partition memberships ─────────────────────────────────────────

    @abstractmethod
    async def assign_partition(self, assignment: UserPartition) -> UserPartition: ...

    @abstractmethod
    async def remove_partition(self, user_id: int, partition: str) -> bool: ...

    @abstractmethod
    async def list_user_partitions(self, user_id: int) -> list[UserPartition]: ...

    @abstractmethod
    async def list_partition_users(self, partition: str) -> list[UserPartition]: ...

    @abstractmethod
    async def update_partition_role(self, user_id: int, partition: str, role: str) -> bool: ...
