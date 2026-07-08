"""Phase 7F — PgOIDCSessionRepository against a real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from core.models.user import OIDCSession, User
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _seed_user(store: PostgresStore) -> User:
    return await store.user_repo.create_user(
        User(display_name="OIDC user", external_user_id="kc-oidc-sub"),
    )


def _session(
    user_id: int,
    token_hash: str = "deadbeef",
    *,
    sub: str = "kc-oidc-sub",
    sid: str | None = "sess-1",
    revoked: bool = False,
    expires_in: timedelta = timedelta(hours=1),
) -> OIDCSession:
    # The ``oidc_sessions`` columns are TIMESTAMP WITHOUT TIME ZONE — the
    # OIDCSession defaults are tz-aware UTC. Production callers strip tzinfo
    # before insert; mirroring that here keeps the repo a thin pass-through
    # (the timezone-column mismatch is a separate carryover from the legacy
    # schema and not in scope for Phase 7F).
    now = datetime.now(UTC).replace(tzinfo=None)
    revoked_at = now if revoked else None
    return OIDCSession(
        session_token_hash=token_hash,
        user_id=user_id,
        sub=sub,
        sid=sid,
        id_token_encrypted=b"id-token-bytes",
        access_token_encrypted=b"access-token-bytes",
        refresh_token_encrypted=b"refresh-token-bytes",
        access_token_expires_at=now + expires_in,
        session_expires_at=now + expires_in,
        created_at=now,
        revoked_at=revoked_at,
    )


class TestCreateGet:
    async def test_create_returns_assigned_id(self, postgres_store: PostgresStore):
        user = await _seed_user(postgres_store)
        created = await postgres_store.oidc_session_repo.create_session(
            _session(user.id),
        )
        assert created.id > 0
        # encrypted byte payloads round-trip verbatim — the repo doesn't
        # try to crypt them, that lives in the auth service.
        assert created.id_token_encrypted == b"id-token-bytes"
        assert created.refresh_token_encrypted == b"refresh-token-bytes"

    async def test_get_by_token_hash(self, postgres_store: PostgresStore):
        user = await _seed_user(postgres_store)
        await postgres_store.oidc_session_repo.create_session(
            _session(user.id, token_hash="hash-abc"),
        )
        fetched = await postgres_store.oidc_session_repo.get_by_token_hash("hash-abc")
        assert fetched is not None
        assert fetched.user_id == user.id

    async def test_revoked_session_hidden_from_lookup(
        self,
        postgres_store: PostgresStore,
    ):
        user = await _seed_user(postgres_store)
        await postgres_store.oidc_session_repo.create_session(
            _session(user.id, token_hash="hash-revoked", revoked=True),
        )
        assert await postgres_store.oidc_session_repo.get_by_token_hash("hash-revoked") is None

    async def test_expired_session_hidden_from_lookup(
        self,
        postgres_store: PostgresStore,
    ):
        user = await _seed_user(postgres_store)
        await postgres_store.oidc_session_repo.create_session(
            _session(user.id, token_hash="hash-expired", expires_in=timedelta(seconds=-60)),
        )
        assert await postgres_store.oidc_session_repo.get_by_token_hash("hash-expired") is None


class TestRevoke:
    async def test_revoke_by_sid_marks_all(self, postgres_store: PostgresStore):
        user = await _seed_user(postgres_store)
        await postgres_store.oidc_session_repo.create_session(
            _session(user.id, token_hash="t1", sid="shared-sid"),
        )
        await postgres_store.oidc_session_repo.create_session(
            _session(user.id, token_hash="t2", sid="shared-sid"),
        )
        count = await postgres_store.oidc_session_repo.revoke_by_sid("shared-sid")
        assert count == 2
        assert await postgres_store.oidc_session_repo.get_by_token_hash("t1") is None

    async def test_revoke_by_sid_missing_returns_zero(
        self,
        postgres_store: PostgresStore,
    ):
        assert await postgres_store.oidc_session_repo.revoke_by_sid("never") == 0

    async def test_revoke_by_user(self, postgres_store: PostgresStore):
        user = await _seed_user(postgres_store)
        await postgres_store.oidc_session_repo.create_session(
            _session(user.id, token_hash="u-t1"),
        )
        revoked = await postgres_store.oidc_session_repo.revoke_by_user(user.id)
        assert revoked == 1
        assert await postgres_store.oidc_session_repo.get_by_token_hash("u-t1") is None


class TestExpiry:
    async def test_delete_expired_only_removes_long_dead(
        self,
        postgres_store: PostgresStore,
    ):
        user = await _seed_user(postgres_store)
        # Repo keeps a 7-day grace window after expiry — see the
        # implementation note in oidc_session_repo.py. A row that just
        # expired must NOT be deleted yet.
        await postgres_store.oidc_session_repo.create_session(
            _session(user.id, token_hash="recently-expired", expires_in=timedelta(seconds=-60)),
        )
        removed = await postgres_store.oidc_session_repo.delete_expired()
        assert removed == 0
