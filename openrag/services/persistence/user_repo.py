"""Postgres implementation of :class:`UserRepository`.

Backs the ``users`` table (and, when the post-refactoring ``api_keys``
table lands, ``api_keys``). The legacy
:class:`components.indexer.vectordb.utils.PartitionFileManager` exposed
eleven user-shaped methods (``create_user``, ``get_user_by_id``,
``get_user_by_token``, ``delete_user``, ``update_user``, ``list_users``,
``regenerate_user_token``, ``user_exists``, ``get_user_by_external_id``,
``update_user_fields``, ``_ensure_admin_user``) which map onto this class.
The six partition-membership methods moved to
:class:`~services.persistence.partition_membership_repo.PgPartitionMembershipRepository`
(7A.2 one-repo-per-entity layout). This class still *reads*
``partition_memberships`` via :meth:`_fetch_memberships` to hydrate the
``User`` aggregate's ``partitions`` field — a read-only denormalisation
inside the user aggregate boundary, not membership management.

Notes on the schema vs. the port:

* The port :class:`~openrag.core.models.user.User` model carries
  ``password_hash``, ``is_active`` and ``updated_at`` fields that have
  no column today. They are treated as ``None`` / ``True`` / ``created_at``
  respectively at the boundary so the domain shape stays useful for
  callers.
* The ``UserRepository`` port also defines four ``api_key_*`` methods.
  OpenRAG currently stores one hashed token in ``users.token`` — a real
  ``api_keys`` table is on the post-refactoring roadmap. Until then the
  api-key methods raise :class:`NotImplementedError` to signal the gap
  loudly rather than silently returning empty lists.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.models.user import ApiKey, PartitionRole, User, UserPartition
from core.ports.user_repo import UserRepository

if TYPE_CHECKING:
    import asyncpg


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of a token string.

    Matches the legacy :meth:`PartitionFileManager.hash_token` so existing
    rows continue to validate against the same hash. Exposed at module
    level so callers (e.g. auth middleware) can hash before lookup
    without instantiating the repo.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PgUserRepository(UserRepository):
    """asyncpg-backed implementation of :class:`UserRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ── User CRUD ────────────────────────────────────────────────────

    async def create_user(self, user: User) -> User:
        """Insert a user row and return it with its assigned PK.

        ``User.password_hash`` is dropped because the column does not
        exist yet — when password auth lands we'll add the column and
        wire it here. ``token`` / ``token_hash`` should be set out-of-band
        via :meth:`set_user_token`; this method does NOT generate one.

        ``external_user_id`` is normalized: an empty string is coerced to
        ``NULL`` so the UNIQUE constraint allows multiple users with no
        external id, instead of raising a UniqueViolation on the second
        insert.
        """
        row = await self.pool.fetchrow(
            """
            INSERT INTO users (display_name, external_user_id, email,
                               is_admin, file_quota, file_count, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, NOW()))
            RETURNING *
            """,
            user.display_name,
            (user.external_user_id.strip() or None) if user.external_user_id else None,
            (user.email.strip().lower() if user.email else None),
            user.is_admin,
            user.file_quota,
            user.file_count,
            user.created_at,
        )
        return self._row_to_user(row)

    async def get_user(self, user_id: int) -> User | None:
        row = await self.pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if row is None:
            return None
        memberships = await self._fetch_memberships(user_id)
        return self._row_to_user(row, memberships)

    async def get_user_by_email(self, email: str) -> User | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE email = $1",
            email.strip().lower(),
        )
        if row is None:
            return None
        memberships = await self._fetch_memberships(row["id"])
        return self._row_to_user(row, memberships)

    async def get_user_by_token(self, token_hash: str) -> User | None:
        """Lookup by the SHA-256 hash of the bearer token.

        The auth middleware hashes the raw token before calling this, so
        the repo never sees plaintext.
        """
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE token = $1",
            token_hash,
        )
        if row is None:
            return None
        memberships = await self._fetch_memberships(row["id"])
        return self._row_to_user(row, memberships)

    async def get_user_by_external_id(self, external_id: str) -> User | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE external_user_id = $1",
            external_id,
        )
        if row is None:
            return None
        memberships = await self._fetch_memberships(row["id"])
        return self._row_to_user(row, memberships)

    async def list_users(self, offset: int = 0, limit: int = 50) -> list[User]:
        rows = await self.pool.fetch(
            "SELECT * FROM users ORDER BY id LIMIT $1 OFFSET $2",
            limit,
            offset,
        )
        return [self._row_to_user(r) for r in rows]

    async def update_user(self, user_id: int, **fields: Any) -> User | None:
        """Patch fields on a user row.

        Silently ignores unknown columns — keeps the call site forgiving
        when the domain model carries fields the schema does not have
        yet (``password_hash``, ``is_active``, ``updated_at``).
        """
        allowed = {
            "display_name",
            "external_user_id",
            "email",
            "is_admin",
            "file_quota",
            "file_count",
            "token",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "email" and isinstance(value, str):
                value = value.strip().lower()
            params.append(value)
            sets.append(f"{key} = ${len(params)}")
        if not sets:
            return await self.get_user(user_id)
        params.append(user_id)
        row = await self.pool.fetchrow(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ${len(params)} RETURNING *",
            *params,
        )
        if row is None:
            return None
        memberships = await self._fetch_memberships(user_id)
        return self._row_to_user(row, memberships)

    async def delete_user(self, user_id: int) -> bool:
        result = await self.pool.execute("DELETE FROM users WHERE id = $1", user_id)
        return result.endswith(" 1")

    async def count_users(self) -> int:
        return await self.pool.fetchval("SELECT COUNT(*)::int FROM users")

    # ── API keys (stub — table not yet shipped) ──────────────────────

    async def create_api_key(self, key: ApiKey) -> ApiKey:
        raise NotImplementedError(
            "api_keys table is on the post-refactoring roadmap; use users.token until then.",
        )

    async def get_api_keys_by_prefix(self, prefix: str) -> list[ApiKey]:
        raise NotImplementedError(
            "api_keys table is on the post-refactoring roadmap; use users.token until then.",
        )

    async def list_api_keys_for_user(self, user_id: int) -> list[ApiKey]:
        raise NotImplementedError(
            "api_keys table is on the post-refactoring roadmap; use users.token until then.",
        )

    async def delete_api_key(self, key_id: str) -> bool:
        raise NotImplementedError(
            "api_keys table is on the post-refactoring roadmap; use users.token until then.",
        )

    # ── Legacy method names used by the Phase 7C shim ────────────────

    async def create_legacy_user(
        self,
        display_name: str | None,
        external_user_id: str | None,
        email: str | None,
        is_admin: bool,
        file_quota: int | None,
    ) -> dict:
        """TODO(phase-9): remove. Mirror of legacy ``create_user``.

        Generates a plain ``or-`` token, stores its hash, returns the
        plaintext exactly once (it is never persisted unhashed).
        """
        plaintext = f"or-{secrets.token_hex(16)}"
        token_hash = _hash_token(plaintext)
        row = await self.pool.fetchrow(
            """
            INSERT INTO users (display_name, external_user_id, email,
                               token, is_admin, file_quota, file_count, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, 0, NOW())
            RETURNING *
            """,
            display_name,
            (external_user_id.strip() or None) if external_user_id else None,
            (email.strip().lower() if email else None),
            token_hash,
            is_admin,
            file_quota,
        )
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "external_user_id": row["external_user_id"],
            "email": row["email"],
            "token": plaintext,
            "is_admin": row["is_admin"],
            "file_quota": row["file_quota"],
            "file_count": row["file_count"],
        }

    async def regenerate_user_token(self, user_id: int) -> dict | None:
        """TODO(phase-9): remove. Rotate ``users.token`` and surface the plaintext."""
        plaintext = f"or-{secrets.token_hex(16)}"
        token_hash = _hash_token(plaintext)
        row = await self.pool.fetchrow(
            """
            UPDATE users SET token = $2
            WHERE id = $1
            RETURNING *
            """,
            user_id,
            token_hash,
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "external_user_id": row["external_user_id"],
            "token": plaintext,
            "is_admin": row["is_admin"],
            "file_quota": row["file_quota"],
            "file_count": row["file_count"],
        }

    async def get_user_by_token_plain(self, token: str) -> dict | None:
        """TODO(phase-9): remove. Hash + lookup + serialise to legacy dict shape."""
        return await self.get_user_dict_by_id(
            await self.pool.fetchval(
                "SELECT id FROM users WHERE token = $1",
                _hash_token(token),
            ),
        )

    async def get_user_dict_by_id(self, user_id: int | None) -> dict | None:
        """TODO(phase-9): remove. Legacy dict shape with ``memberships`` list."""
        if user_id is None:
            return None
        row = await self.pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if row is None:
            return None
        memberships = await self.pool.fetch(
            "SELECT * FROM partition_memberships WHERE user_id = $1 ORDER BY added_at",
            user_id,
        )
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "external_user_id": row["external_user_id"],
            "email": row["email"],
            "is_admin": row["is_admin"],
            "file_quota": row["file_quota"],
            "file_count": row["file_count"],
            "memberships": [
                {
                    "partition": m["partition_name"],
                    "role": m["role"],
                    "added_at": m["added_at"].isoformat() if m["added_at"] else None,
                }
                for m in memberships
            ],
        }

    async def get_user_by_external_id_dict(self, external_user_id: str) -> dict | None:
        """TODO(phase-9): remove. Legacy dict shape, lookup by OIDC sub claim."""
        row = await self.pool.fetchrow(
            "SELECT id FROM users WHERE external_user_id = $1",
            external_user_id,
        )
        return await self.get_user_dict_by_id(row["id"]) if row else None

    async def list_users_dict(self) -> list[dict]:
        """TODO(phase-9): remove. Legacy list shape used by /users/ endpoint."""
        rows = await self.pool.fetch("SELECT * FROM users ORDER BY id")
        return [
            {
                "id": r["id"],
                "display_name": r["display_name"],
                "external_user_id": r["external_user_id"],
                "is_admin": r["is_admin"],
                "file_quota": r["file_quota"],
                "file_count": r["file_count"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]

    async def user_exists(self, user_id: int) -> bool:
        """TODO(phase-9): remove."""
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM users WHERE id = $1)",
            user_id,
        )

    # Whitelist mirrored from the legacy PartitionFileManager. Three
    # layers (startup validator, claim parser, repo) all enforce the
    # same set as defence-in-depth against an OIDC claim mapping that
    # would otherwise let a remote IdP rewrite arbitrary user columns.
    _OIDC_WRITABLE_USER_FIELDS = frozenset({"display_name", "email"})

    async def update_user_fields(self, user_id: int, fields: dict[str, Any]) -> None:
        """TODO(phase-9): remove. Strict-whitelist update for the OIDC claim mapper."""
        if not fields:
            return
        bad = set(fields) - self._OIDC_WRITABLE_USER_FIELDS
        if bad:
            raise ValueError(f"Cannot update non-whitelisted user fields: {sorted(bad)}")
        cleaned = {k: v for k, v in fields.items() if v is not None}
        if not cleaned:
            return
        if "email" in cleaned and isinstance(cleaned["email"], str):
            cleaned["email"] = cleaned["email"].strip().lower()
        sets: list[str] = []
        params: list[Any] = []
        for key, value in cleaned.items():
            params.append(value)
            sets.append(f"{key} = ${len(params)}")
        params.append(user_id)
        result = await self.pool.execute(
            f"UPDATE users SET {', '.join(sets)} WHERE id = ${len(params)}",
            *params,
        )
        if not result.endswith(" 1"):
            raise ValueError(f"User {user_id} not found")

    async def ensure_admin_user(self, admin_token: str | None) -> str:
        """TODO(phase-9): remove. Bootstrap mirror of the legacy admin-bootstrap.

        Ensures ``users.id = 1`` exists with ``is_admin = TRUE`` and the
        token hash matching ``admin_token``. Generates a token if none
        is supplied. Returns whichever plaintext token is now valid.
        """
        plaintext = admin_token or f"or-{secrets.token_hex(16)}"
        token_hash = _hash_token(plaintext)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow("SELECT id FROM users WHERE id = 1")
                if existing is None:
                    await conn.execute(
                        """
                        INSERT INTO users (id, display_name, token, is_admin, file_count, created_at)
                        VALUES (1, 'Admin', $1, TRUE, 0, NOW())
                        """,
                        token_hash,
                    )
                    # Keep the sequence ahead of the explicit id=1 insert so
                    # subsequent `INSERT INTO users` calls don't collide.
                    await conn.execute(
                        "SELECT setval(pg_get_serial_sequence('users','id'), GREATEST(1, (SELECT MAX(id) FROM users)))"
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET is_admin = TRUE, token = $1 WHERE id = 1",
                        token_hash,
                    )
        return plaintext

    # ── Helpers ──────────────────────────────────────────────────────

    async def _fetch_memberships(self, user_id: int) -> list[UserPartition]:
        rows = await self.pool.fetch(
            "SELECT * FROM partition_memberships WHERE user_id = $1 ORDER BY added_at",
            user_id,
        )
        return [self._row_to_user_partition(r) for r in rows]

    @staticmethod
    def _row_to_user(
        row: asyncpg.Record,
        memberships: list[UserPartition] | None = None,
    ) -> User:
        # `password_hash`, `is_active`, `updated_at` do not exist on the
        # current schema; fall back to safe defaults so the domain model
        # stays consistent even though the underlying row is narrower.
        created = row["created_at"]
        return User(
            id=row["id"],
            display_name=row["display_name"],
            external_user_id=row["external_user_id"],
            email=row["email"],
            password_hash=None,
            is_admin=row["is_admin"],
            is_active=True,
            file_quota=row["file_quota"],
            file_count=row["file_count"],
            created_at=created,
            updated_at=created,
            partitions=memberships or [],
        )

    @staticmethod
    def _row_to_user_partition(row: asyncpg.Record) -> UserPartition:
        return UserPartition(
            user_id=row["user_id"],
            partition=row["partition_name"],
            role=PartitionRole(row["role"]),
            added_at=row["added_at"],
        )


__all__ = ["PgUserRepository", "_hash_token"]
