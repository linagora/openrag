"""User repository interface — users and API keys.

Partition memberships moved to
:class:`~openrag.core.ports.partition_membership_repo.PartitionMembershipRepository`
(7A.2 one-repo-per-entity layout).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from core.models.user import ApiKey, FileSlotGrant, User


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
    async def get_users_by_ids(self, user_ids: list[int]) -> list[User]: ...

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

    # ── File-quota reservations (#664) ────────────────────────────────
    #
    # ``users.file_count`` counts a user's files plus the slots they hold for
    # uploads still in flight. A reservation is consumed by the catalog insert
    # (``add_file_to_partition`` with its id) or released; both are idempotent,
    # so any number of cleanup paths may release the same one safely.

    @abstractmethod
    async def try_reserve_file_slot(
        self,
        user_id: int,
        reservation_id: str,
        *,
        default_quota: int,
    ) -> FileSlotGrant:
        """Take one slot under ``reservation_id`` if the user's quota allows it.

        The check and the increment are a single conditional UPDATE, so
        concurrent callers can never take more slots than the quota leaves.
        Admins and users whose resolved quota is below zero get ``UNLIMITED``
        and no reservation; a ``NULL`` per-user quota resolves to
        ``default_quota``.
        """

    @abstractmethod
    async def release_file_slot(self, reservation_id: str) -> bool:
        """Give back the slot held under ``reservation_id``.

        Returns whether a slot was released. A reservation already consumed or
        released is left alone, so a second release is a no-op.
        """

    @abstractmethod
    async def release_stale_file_slots(
        self,
        *,
        live_ids: list[str],
        settled_ids: list[str],
        created_before: datetime,
    ) -> int:
        """Release reservations no live task can still consume.

        Keeps every id in ``live_ids``. Releases the rest when they are in
        ``settled_ids`` or older than ``created_before``. Returns how many
        were released.
        """

    @abstractmethod
    async def count_file_slot_reservations(self, user_id: int) -> int:
        """How many of the user's slots are held by uploads still in flight."""

    # ── API keys ──────────────────────────────────────────────────────

    @abstractmethod
    async def create_api_key(self, key: ApiKey) -> ApiKey: ...

    @abstractmethod
    async def get_api_keys_by_prefix(self, prefix: str) -> list[ApiKey]: ...

    @abstractmethod
    async def list_api_keys_for_user(self, user_id: int) -> list[ApiKey]: ...

    @abstractmethod
    async def delete_api_key(self, key_id: str) -> bool: ...
