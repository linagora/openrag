"""User repository interface — users and API keys.

Partition memberships moved to
:class:`~openrag.core.ports.partition_membership_repo.PartitionMembershipRepository`
(7A.2 one-repo-per-entity layout).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openrag.core.models.user import ApiKey, User


class UserRepository(ABC):
    """CRUD operations for users and API keys.

    Supports three auth modes:
    - OIDC/SSO: lookup by external_user_id
    - API token: lookup by token hash (legacy or- tokens)
    - Password + JWT: lookup by email, verify password hash
    """

    # ── User CRUD ─────────────────────────────────────────────────────

    @abstractmethod
    async def create_user(self, user: User) -> User: ...

    @abstractmethod
    async def get_user(self, user_id: int) -> User | None: ...

    @abstractmethod
    async def get_user_by_email(self, email: str) -> User | None: ...

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

    @abstractmethod
    async def count_users(self) -> int: ...

    # ── API keys ──────────────────────────────────────────────────────

    @abstractmethod
    async def create_api_key(self, key: ApiKey) -> ApiKey: ...

    @abstractmethod
    async def get_api_keys_by_prefix(self, prefix: str) -> list[ApiKey]: ...

    @abstractmethod
    async def list_api_keys_for_user(self, user_id: int) -> list[ApiKey]: ...

    @abstractmethod
    async def delete_api_key(self, key_id: str) -> bool: ...
