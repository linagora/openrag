"""Postgres implementation of :class:`OIDCSessionRepository`.

Backs the ``oidc_sessions`` table — the persistence side of the OIDC
authorization-code + PKCE flow. The legacy
:class:`components.indexer.vectordb.utils.PartitionFileManager` exposed
seven OIDC methods (``create_oidc_session``, ``get_oidc_session_by_token``,
``get_oidc_session_by_id``, ``update_oidc_session_tokens``,
``revoke_oidc_sessions_by_sid``, ``revoke_oidc_session_by_id``,
``cleanup_expired_oidc_sessions``) that all land on this class.

Tokens (``id_token``, ``access_token``, ``refresh_token``) are stored
**Fernet-encrypted** as ``BYTEA``. Encryption and decryption are the
caller's responsibility (see ``components.auth.crypto``) — the repo
treats the bytes as opaque. The plain session cookie value is hashed
(SHA-256) at the caller before being passed in.

Hard expiry is enforced at read-time: a row is hidden once
``session_expires_at`` is in the past or ``revoked_at`` is set, matching
the legacy behaviour. A periodic call to :meth:`delete_expired` keeps
the table bounded; an explicit retention window (7 days past expiry)
mirrors the legacy ``cleanup_expired_oidc_sessions``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.models.user import OIDCSession
from core.ports.oidc_session_repo import OIDCSessionRepository

if TYPE_CHECKING:
    import asyncpg


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of an opaque session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Same retention window the legacy code used — keep expired rows around
# for a week so we can post-mortem broken sessions, then prune.
_EXPIRED_RETENTION = timedelta(days=7)


class PgOIDCSessionRepository(OIDCSessionRepository):
    """asyncpg-backed implementation of :class:`OIDCSessionRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    # ── OIDCSessionRepository port methods ───────────────────────────

    async def create_session(self, session: OIDCSession) -> OIDCSession:
        """Insert a new OIDC session row.

        ``session.session_token_hash`` is treated as already SHA-256
        hashed — the auth service is responsible for hashing the cookie
        value before calling this. If it arrives unhashed the row will
        still insert but no future lookup will find it.
        """
        row = await self.pool.fetchrow(
            """
            INSERT INTO oidc_sessions (
                session_token_hash, user_id, sub, sid,
                id_token_encrypted, access_token_encrypted, refresh_token_encrypted,
                access_token_expires_at, session_expires_at,
                created_at, last_refresh_at, revoked_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                $8, $9,
                COALESCE($10, NOW()), $11, $12
            )
            RETURNING *
            """,
            session.session_token_hash,
            session.user_id,
            session.sub,
            session.sid,
            session.id_token_encrypted,
            session.access_token_encrypted,
            session.refresh_token_encrypted,
            session.access_token_expires_at,
            session.session_expires_at,
            session.created_at,
            session.last_refresh_at,
            session.revoked_at,
        )
        return self._row_to_session(row)

    async def get_by_token_hash(self, token_hash: str) -> OIDCSession | None:
        """Lookup by token hash, filtering out revoked / expired rows."""
        row = await self.pool.fetchrow(
            """
            SELECT * FROM oidc_sessions
            WHERE session_token_hash = $1
              AND revoked_at IS NULL
              AND session_expires_at >= NOW()
            """,
            token_hash,
        )
        return self._row_to_session(row) if row else None

    async def get_by_id(self, session_id: int) -> OIDCSession | None:
        """Lookup by primary key, filtering out revoked / expired rows."""
        row = await self.pool.fetchrow(
            """
            SELECT * FROM oidc_sessions
            WHERE id = $1
              AND revoked_at IS NULL
              AND session_expires_at >= NOW()
            """,
            session_id,
        )
        return self._row_to_session(row) if row else None

    async def get_by_sid(self, sid: str) -> list[OIDCSession]:
        rows = await self.pool.fetch(
            "SELECT * FROM oidc_sessions WHERE sid = $1 ORDER BY created_at",
            sid,
        )
        return [self._row_to_session(r) for r in rows]

    async def update_session(self, session_id: int, **fields: Any) -> OIDCSession | None:
        """Patch a session row by primary key.

        Whitelist matches the columns the auth flow legitimately
        rotates: ``access_token_encrypted``, ``refresh_token_encrypted``,
        ``id_token_encrypted``, ``access_token_expires_at``,
        ``session_expires_at``, ``last_refresh_at``, ``revoked_at``,
        ``sid``. Unknown keys are ignored.
        """
        allowed = {
            "access_token_encrypted",
            "refresh_token_encrypted",
            "id_token_encrypted",
            "access_token_expires_at",
            "session_expires_at",
            "last_refresh_at",
            "revoked_at",
            "sid",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            params.append(value)
            sets.append(f"{key} = ${len(params)}")
        if not sets:
            row = await self.pool.fetchrow(
                "SELECT * FROM oidc_sessions WHERE id = $1",
                session_id,
            )
            return self._row_to_session(row) if row else None
        params.append(session_id)
        row = await self.pool.fetchrow(
            f"UPDATE oidc_sessions SET {', '.join(sets)} WHERE id = ${len(params)} RETURNING *",
            *params,
        )
        return self._row_to_session(row) if row else None

    async def revoke_session(self, session_id: int) -> bool:
        """Mark a single session revoked (RP-initiated logout)."""
        result = await self.pool.execute(
            """
            UPDATE oidc_sessions SET revoked_at = NOW()
            WHERE id = $1 AND revoked_at IS NULL
            """,
            session_id,
        )
        return result.endswith(" 1")

    async def revoke_by_sid(self, sid: str) -> int:
        """Mark every non-revoked session with this OIDC ``sid`` revoked.

        Used by the OIDC back-channel logout flow: the IdP POSTs a
        signed logout token whose ``sid`` claim names the session(s) to
        terminate; we mark them revoked and return the affected count.
        """
        result = await self.pool.execute(
            """
            UPDATE oidc_sessions SET revoked_at = NOW()
            WHERE sid = $1 AND revoked_at IS NULL
            """,
            sid,
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def revoke_by_user(self, user_id: int) -> int:
        """Revoke every non-revoked session belonging to a user."""
        result = await self.pool.execute(
            """
            UPDATE oidc_sessions SET revoked_at = NOW()
            WHERE user_id = $1 AND revoked_at IS NULL
            """,
            user_id,
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def delete_expired(self) -> int:
        """Hard-delete rows expired more than 7 days ago. Returns count."""
        # The ``session_expires_at`` column is TIMESTAMP WITHOUT TIME ZONE,
        # so the cutoff has to be tz-naive UTC — asyncpg refuses to bind a
        # tz-aware value against a tz-naive column.
        cutoff = datetime.now(UTC).replace(tzinfo=None) - _EXPIRED_RETENTION
        result = await self.pool.execute(
            "DELETE FROM oidc_sessions WHERE session_expires_at < $1",
            cutoff,
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    # ── Legacy method names used by the Phase 7C shim ────────────────

    async def create_oidc_session(  # noqa: PLR0913 — legacy signature pinned
        self,
        *,
        user_id: int,
        sub: str,
        sid: str | None,
        session_token_plain: str,
        id_token_encrypted: bytes | None,
        access_token_encrypted: bytes | None,
        refresh_token_encrypted: bytes | None,
        access_token_expires_at: datetime,
        session_expires_at: datetime,
    ) -> dict:
        """TODO(phase-9): remove. Legacy interface — hashes the plaintext cookie."""
        token_hash = _hash_token(session_token_plain)
        row = await self.pool.fetchrow(
            """
            INSERT INTO oidc_sessions (
                session_token_hash, user_id, sub, sid,
                id_token_encrypted, access_token_encrypted, refresh_token_encrypted,
                access_token_expires_at, session_expires_at, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
            RETURNING *
            """,
            token_hash,
            user_id,
            sub,
            sid,
            id_token_encrypted,
            access_token_encrypted,
            refresh_token_encrypted,
            access_token_expires_at,
            session_expires_at,
        )
        return self._row_to_dict(row)

    async def get_oidc_session_by_token(self, session_token_plain: str) -> dict | None:
        """TODO(phase-9): remove. Returns ``None`` for revoked / expired rows."""
        row = await self.pool.fetchrow(
            """
            SELECT * FROM oidc_sessions
            WHERE session_token_hash = $1
              AND revoked_at IS NULL
              AND session_expires_at >= NOW()
            """,
            _hash_token(session_token_plain),
        )
        return self._row_to_dict(row) if row else None

    async def get_oidc_session_by_id(self, session_id: int) -> dict | None:
        """TODO(phase-9): remove. Same hidden-row rules as the legacy code."""
        row = await self.pool.fetchrow(
            """
            SELECT * FROM oidc_sessions
            WHERE id = $1
              AND revoked_at IS NULL
              AND session_expires_at >= NOW()
            """,
            session_id,
        )
        return self._row_to_dict(row) if row else None

    async def update_oidc_session_tokens(
        self,
        *,
        session_id: int,
        access_token_encrypted: bytes,
        refresh_token_encrypted: bytes | None,
        access_token_expires_at: datetime,
    ) -> None:
        """TODO(phase-9): remove. Atomic token-rotation with row lock.

        ``SELECT ... FOR UPDATE`` serialises concurrent refresh callers on
        the same session row — Postgres only. The wider stampede guard in
        :mod:`components.auth.refresh` short-circuits before this is even
        called in the common case.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id FROM oidc_sessions WHERE id = $1 FOR UPDATE",
                    session_id,
                )
                if row is None:
                    raise ValueError(f"oidc_session id={session_id} does not exist")
                if refresh_token_encrypted is not None:
                    await conn.execute(
                        """
                        UPDATE oidc_sessions
                        SET access_token_encrypted = $2,
                            refresh_token_encrypted = $3,
                            access_token_expires_at = $4,
                            last_refresh_at = NOW()
                        WHERE id = $1
                        """,
                        session_id,
                        access_token_encrypted,
                        refresh_token_encrypted,
                        access_token_expires_at,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE oidc_sessions
                        SET access_token_encrypted = $2,
                            access_token_expires_at = $3,
                            last_refresh_at = NOW()
                        WHERE id = $1
                        """,
                        session_id,
                        access_token_encrypted,
                        access_token_expires_at,
                    )

    async def revoke_oidc_sessions_by_sid(self, sid: str) -> int:
        """TODO(phase-9): remove. Alias for :meth:`revoke_by_sid`."""
        return await self.revoke_by_sid(sid)

    async def revoke_oidc_session_by_id(self, session_id: int) -> None:
        """TODO(phase-9): remove. Returns nothing; legacy contract."""
        await self.revoke_session(session_id)

    async def cleanup_expired_oidc_sessions(self) -> int:
        """TODO(phase-9): remove. Alias for :meth:`delete_expired`."""
        return await self.delete_expired()

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_session(row: asyncpg.Record) -> OIDCSession:
        return OIDCSession(
            id=row["id"],
            session_token_hash=row["session_token_hash"],
            user_id=row["user_id"],
            sid=row["sid"],
            sub=row["sub"],
            id_token_encrypted=row["id_token_encrypted"],
            access_token_encrypted=row["access_token_encrypted"],
            refresh_token_encrypted=row["refresh_token_encrypted"],
            access_token_expires_at=row["access_token_expires_at"],
            session_expires_at=row["session_expires_at"],
            created_at=row["created_at"],
            last_refresh_at=row["last_refresh_at"],
            revoked_at=row["revoked_at"],
        )

    @staticmethod
    def _row_to_dict(row: asyncpg.Record) -> dict:
        """Legacy dict shape — matches PartitionFileManager._oidc_session_to_dict.

        Encrypted blobs are passed through untouched; the caller decrypts.
        """
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "sub": row["sub"],
            "sid": row["sid"],
            "id_token_encrypted": row["id_token_encrypted"],
            "access_token_encrypted": row["access_token_encrypted"],
            "refresh_token_encrypted": row["refresh_token_encrypted"],
            "access_token_expires_at": row["access_token_expires_at"],
            "session_expires_at": row["session_expires_at"],
            "created_at": row["created_at"],
            "last_refresh_at": row["last_refresh_at"],
            "revoked_at": row["revoked_at"],
        }


__all__ = ["PgOIDCSessionRepository", "_hash_token"]
