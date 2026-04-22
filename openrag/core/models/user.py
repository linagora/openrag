"""User, role, partition assignment, API key, and OIDC session domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class PartitionRole(str, Enum):
    VIEWER = "viewer"
    EDITOR = "editor"
    OWNER = "owner"


class User(BaseModel):
    """An OpenRAG user account.

    Supports three auth modes:
    - OIDC/SSO: user matched by external_user_id (Keycloak sub claim),
      session managed via OIDCSession. For browser users.
    - API token: opaque or- prefixed token, SHA-256 hashed in DB.
      For scripts, CI/CD, programmatic access. Legacy mode.
    - Password + JWT: user logs in with email/password, receives
      JWT access + refresh tokens. For programmatic access and
      users without Keycloak.
    """

    id: int = 0
    display_name: str | None = None
    external_user_id: str | None = None
    email: str | None = None
    password_hash: str | None = Field(None, exclude=True, repr=False)
    is_admin: bool = False
    is_active: bool = True
    file_quota: int | None = None
    file_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    partitions: list[UserPartition] = Field(default_factory=list)


class UserPartition(BaseModel):
    """A user's membership in a partition with a role."""

    user_id: int
    partition: str
    role: PartitionRole = PartitionRole.VIEWER
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ApiKey(BaseModel):
    """An API key for programmatic access.

    The raw key is shown once on creation (key_prefix + random hex).
    Only the hash is stored in DB.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: int = 0
    key_hash: str = ""
    key_prefix: str = ""
    name: str = ""
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None


class TokenPayload(BaseModel):
    """JWT token claims payload."""

    sub: str = ""
    type: str = "access"
    role: str = "user"
    exp: int = 0


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
