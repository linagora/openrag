"""User, role, partition assignment, and OIDC session domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class PartitionRole(str, Enum):
    VIEWER = "viewer"
    EDITOR = "editor"
    OWNER = "owner"


class User(BaseModel):
    """An OpenRAG user account.

    Supports two auth modes:
    - Token mode: user has a hashed token in DB (token field on SQLAlchemy model)
    - OIDC mode: user matched by external_user_id (Keycloak sub claim),
      session managed via OIDCSession
    """

    id: int = 0
    display_name: str | None = None
    external_user_id: str | None = None
    email: str | None = None
    is_admin: bool = False
    file_quota: int | None = None
    file_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    partitions: list[UserPartition] = Field(default_factory=list)


class UserPartition(BaseModel):
    """A user's membership in a partition with a role."""

    user_id: int
    partition: str
    role: PartitionRole = PartitionRole.VIEWER
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OIDCSession(BaseModel):
    """An active OIDC session linking a user to IdP tokens.

    Session token is opaque (stored hashed in DB).
    IdP tokens (access, refresh, id) are Fernet-encrypted in DB.
    """

    id: int = 0
    session_token_hash: str = ""
    user_id: int = 0
    sid: str | None = None
    sub: str = ""
    access_token_expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    session_expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_refresh_at: datetime | None = None
    revoked_at: datetime | None = None
