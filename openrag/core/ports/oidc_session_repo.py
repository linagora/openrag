"""OIDC session repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from openrag.core.models.user import OIDCSession


class OIDCSessionRepository(ABC):
    """CRUD operations for OIDC sessions."""

    @abstractmethod
    async def create_session(self, session: OIDCSession) -> OIDCSession: ...

    @abstractmethod
    async def get_by_token_hash(self, token_hash: str) -> OIDCSession | None: ...

    @abstractmethod
    async def get_by_sid(self, sid: str) -> list[OIDCSession]: ...

    @abstractmethod
    async def update_session(self, session_id: int, **fields) -> OIDCSession | None: ...

    @abstractmethod
    async def revoke_session(self, session_id: int) -> bool: ...

    @abstractmethod
    async def revoke_by_sid(self, sid: str) -> int:
        """Revoke all sessions with a given OIDC session ID (back-channel logout)."""
        ...

    @abstractmethod
    async def revoke_by_user(self, user_id: int) -> int:
        """Revoke all sessions for a user."""
        ...

    @abstractmethod
    async def delete_expired(self) -> int:
        """Delete sessions past their session_expires_at. Returns count."""
        ...
