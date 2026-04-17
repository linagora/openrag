"""Unit tests for the Phase-5 ``AuthMiddleware``.

These tests mount the middleware on a minimal FastAPI app with a ``MagicMock``
``vectordb`` — no Ray, no Postgres, no Milvus. They exercise the decision tree
documented in ``.omc/plans/oidc-auth/plan.md`` §6.1.

Timezone policy: Phase 2 stores session timestamps as naive UTC (``datetime.now()``),
so the refresh helper compares naive datetimes. These tests follow suit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from components.auth.middleware import AuthMiddleware, is_ui_path
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vectordb_mock(
    *,
    user=None,
    user_by_token=None,
    session=None,
    partitions=None,
):
    """Return a MagicMock exposing the Ray-actor surface used by the middleware.

    Every ``.remote(...)`` call returns a *coroutine* since the middleware
    awaits it.
    """
    mock = MagicMock()

    mock.get_user = MagicMock()
    mock.get_user.remote = AsyncMock(return_value=user or {"id": 1, "display_name": "Admin"})

    mock.get_user_by_token = MagicMock()
    mock.get_user_by_token.remote = AsyncMock(return_value=user_by_token)

    mock.get_oidc_session_by_token = MagicMock()
    mock.get_oidc_session_by_token.remote = AsyncMock(return_value=session)

    mock.list_user_partitions = MagicMock()
    mock.list_user_partitions.remote = AsyncMock(return_value=partitions or [])

    mock.revoke_oidc_session_by_id = MagicMock()
    mock.revoke_oidc_session_by_id.remote = AsyncMock(return_value=None)

    mock.update_oidc_session_tokens = MagicMock()
    mock.update_oidc_session_tokens.remote = AsyncMock(return_value=None)

    return mock


def _build_app(vectordb_mock) -> FastAPI:
    """Construct a FastAPI app with the middleware under test."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware, get_vectordb=lambda: vectordb_mock)

    @app.get("/")
    async def root(request: Request):
        return {"user": request.state.user["id"]}

    @app.get("/v1/chat/completions")
    async def chat(request: Request):
        return {"user": request.state.user["id"]}

    @app.get("/indexer/foo")
    async def indexer_foo(request: Request):
        return {"user": request.state.user["id"]}

    @app.get("/users/info")
    async def users_info(request: Request):
        return {"user": request.state.user["id"]}

    @app.get("/static/foo.pdf")
    async def static_file(request: Request):
        return {"user": request.state.user["id"]}

    @app.get("/health_check")
    async def hc():
        return "ok"

    return app


# ---------------------------------------------------------------------------
# is_ui_path — pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/", True),
        ("/static/x.pdf", True),
        ("/static", True),
        ("/v1/chat/completions", False),
        ("/v1/models", False),
        ("/indexer/add_file", False),
        ("/search/foo", False),
        ("/users/info", False),
        ("/partition/foo", False),
        ("/workspaces/list", False),
        ("/queue/info", False),
        ("/extract/something", False),
        ("/actors/", False),
        ("/monitoring/status", False),
        ("/tools/execute", False),
        ("/unknown/thing", False),  # default: not UI (avoid redirect loops)
    ],
)
def test_is_ui_path(path, expected):
    assert is_ui_path(path) is expected


# ---------------------------------------------------------------------------
# Token mode (legacy) — must preserve 403 + legacy error bodies
# ---------------------------------------------------------------------------


class TestTokenModeLegacy:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "token")
        monkeypatch.setenv("AUTH_TOKEN", "configured-admin-token")

    def test_bearer_valid_returns_200(self):
        vdb = _make_vectordb_mock(user_by_token={"id": 7, "display_name": "U"})
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer good-token"},
            )
        assert r.status_code == 200
        assert r.json() == {"user": 7}

    def test_bearer_invalid_returns_403(self):
        vdb = _make_vectordb_mock(user_by_token=None)
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer bogus"},
            )
        assert r.status_code == 403
        assert r.json() == {"detail": "Invalid token"}

    def test_missing_token_returns_403(self):
        vdb = _make_vectordb_mock()
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get("/v1/chat/completions")
        assert r.status_code == 403
        assert r.json() == {"detail": "Missing token"}

    def test_bypass_path_open(self):
        vdb = _make_vectordb_mock()
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get("/health_check")
        assert r.status_code == 200


class TestTokenModeDevBypass:
    """AUTH_MODE=token, AUTH_TOKEN unset → all requests resolve to user id=1."""

    def test_no_token_resolves_user_1(self, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "token")
        monkeypatch.delenv("AUTH_TOKEN", raising=False)
        vdb = _make_vectordb_mock(user={"id": 1, "display_name": "Admin"})
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get("/v1/chat/completions")
        assert r.status_code == 200
        assert r.json() == {"user": 1}
        vdb.get_user.remote.assert_awaited_with(1)


# ---------------------------------------------------------------------------
# OIDC mode
# ---------------------------------------------------------------------------


class TestOIDCMode:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "oidc")
        # refresh helper reads OIDC_TOKEN_ENCRYPTION_KEY but we patch the helper
        # in refresh-related tests, so a dummy value is fine.
        monkeypatch.setenv("OIDC_TOKEN_ENCRYPTION_KEY", "dummy")

    def _fresh_session(self, user_id=42):
        """A session whose access_token is still well within lifetime."""
        return {
            "id": 1,
            "user_id": user_id,
            "sub": "sub-abc",
            "sid": "sid-xyz",
            "id_token_encrypted": None,
            "access_token_encrypted": b"enc-access",
            "refresh_token_encrypted": b"enc-refresh",
            "access_token_expires_at": datetime.now() + timedelta(minutes=30),
            "session_expires_at": datetime.now() + timedelta(hours=8),
            "revoked_at": None,
            "last_refresh_at": None,
        }

    # -- cookie session happy path ------------------------------------------

    def test_cookie_valid_and_access_token_fresh_no_refresh(self):
        session = self._fresh_session(user_id=42)
        user = {"id": 42, "display_name": "Alice"}
        vdb = _make_vectordb_mock(user=user, session=session)
        app = _build_app(vdb)
        with TestClient(app) as client:
            client.cookies.set("openrag_session", "plain-cookie")
            r = client.get("/v1/chat/completions")
        assert r.status_code == 200
        assert r.json() == {"user": 42}
        vdb.update_oidc_session_tokens.remote.assert_not_awaited()
        vdb.revoke_oidc_session_by_id.remote.assert_not_awaited()

    def test_cookie_near_expiry_triggers_refresh(self, monkeypatch):
        """access_token within 60s of expiry AND refresh_token present → refresh."""
        session = self._fresh_session(user_id=42)
        # Force the refresh helper to "see" the token as near-expiry.
        session["access_token_expires_at"] = datetime.now() + timedelta(seconds=5)
        user = {"id": 42}
        vdb = _make_vectordb_mock(user=user, session=session)

        # Patch the helper at its import site inside the middleware module
        # to avoid any dependency on a real OIDC client.
        async def fake_refresh(*, session, enc_key, vectordb):
            new_exp = datetime.now() + timedelta(minutes=30)
            await vectordb.update_oidc_session_tokens.remote(
                session_id=session["id"],
                access_token_encrypted=b"new-enc-access",
                refresh_token_encrypted=b"new-enc-refresh",
                access_token_expires_at=new_exp,
            )
            return {
                **session,
                "access_token_encrypted": b"new-enc-access",
                "access_token_expires_at": new_exp,
                "refresh_token_encrypted": b"new-enc-refresh",
            }

        with patch(
            "components.auth.middleware.refresh_session_if_needed",
            side_effect=fake_refresh,
        ):
            app = _build_app(vdb)
            with TestClient(app) as client:
                client.cookies.set("openrag_session", "plain-cookie")
                r = client.get("/v1/chat/completions")

        assert r.status_code == 200
        vdb.update_oidc_session_tokens.remote.assert_awaited()

    def test_cookie_refresh_fails_session_revoked_and_302(self):
        """access_token expired + refresh fails → session revoked, UI request → 302."""
        session = self._fresh_session(user_id=42)
        session["access_token_expires_at"] = datetime.now() - timedelta(minutes=1)
        vdb = _make_vectordb_mock(user=None, session=session)

        async def fake_refresh(*, session, enc_key, vectordb):
            return None  # refresh failed → invalid session

        with patch(
            "components.auth.middleware.refresh_session_if_needed",
            side_effect=fake_refresh,
        ):
            app = _build_app(vdb)
            with TestClient(app) as client:
                client.cookies.set("openrag_session", "plain-cookie")
                r = client.get("/", follow_redirects=False)

        assert r.status_code == 302
        assert r.headers["location"].startswith("/auth/login?next=")
        vdb.revoke_oidc_session_by_id.remote.assert_awaited_with(1)

    # -- bearer fallback ----------------------------------------------------

    def test_bearer_fallback_accepted_in_oidc_mode(self):
        """Programmatic clients keep using ``users.token`` in oidc mode."""
        vdb = _make_vectordb_mock(user_by_token={"id": 9, "display_name": "bot"})
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer ci-token"},
            )
        assert r.status_code == 200
        assert r.json() == {"user": 9}

    # -- unauthenticated branching ------------------------------------------

    def test_no_creds_api_path_returns_401(self):
        vdb = _make_vectordb_mock()
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get("/indexer/foo")
        assert r.status_code == 401
        assert r.json() == {"detail": "Unauthenticated"}

    def test_no_creds_root_path_returns_302(self):
        vdb = _make_vectordb_mock()
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        # ``next`` must preserve the original path+query
        assert r.headers["location"] == "/auth/login?next=%2F"

    def test_no_creds_root_with_query_preserves_next(self):
        vdb = _make_vectordb_mock()
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get("/?foo=bar", follow_redirects=False)
        assert r.status_code == 302
        # %2F / %3F / %3D — full url-encoding
        assert "next=" in r.headers["location"]
        assert "%2F" in r.headers["location"]

    def test_no_creds_static_path_returns_302(self):
        vdb = _make_vectordb_mock()
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get("/static/foo.pdf", follow_redirects=False)
        assert r.status_code == 302

    def test_no_creds_v1_chat_returns_401(self):
        vdb = _make_vectordb_mock()
        app = _build_app(vdb)
        with TestClient(app) as client:
            r = client.get("/v1/chat/completions")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# refresh_session_if_needed — behavioural unit test
# ---------------------------------------------------------------------------


class TestRefreshHelper:
    @pytest.mark.asyncio
    async def test_no_refresh_when_token_fresh(self):
        from components.auth.refresh import refresh_session_if_needed

        session = {
            "id": 1,
            "access_token_expires_at": datetime.now() + timedelta(minutes=30),
            "refresh_token_encrypted": b"foo",
        }
        vdb = MagicMock()
        vdb.update_oidc_session_tokens = MagicMock()
        vdb.update_oidc_session_tokens.remote = AsyncMock()

        out = await refresh_session_if_needed(session=session, enc_key="k", vectordb=vdb)
        assert out is session
        vdb.update_oidc_session_tokens.remote.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_no_refresh_token_returns_none(self):
        from components.auth.refresh import refresh_session_if_needed

        session = {
            "id": 1,
            "access_token_expires_at": datetime.now() - timedelta(minutes=1),
            "refresh_token_encrypted": None,
        }
        vdb = MagicMock()
        out = await refresh_session_if_needed(session=session, enc_key="k", vectordb=vdb)
        assert out is None

    # ------------------------------------------------------------------
    # M1: refresh-token stampede guard
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_refresh_short_circuit_when_last_refresh_recent(self):
        """If another request refreshed <5s ago, reuse the fresh row; do NOT
        hit the IdP again with a refresh_token that has already been rotated."""
        from components.auth import refresh as refresh_mod
        from components.auth.refresh import refresh_session_if_needed

        now = datetime.now()
        fresh_exp = now + timedelta(minutes=30)
        fresh_row = {
            "id": 1,
            "access_token_expires_at": fresh_exp,
            "refresh_token_encrypted": b"new-refresh",
            "access_token_encrypted": b"new-access",
            "last_refresh_at": now,
        }
        stale_session = {
            "id": 1,
            # About to expire → normally we would call the IdP.
            "access_token_expires_at": now + timedelta(seconds=5),
            "refresh_token_encrypted": b"old-refresh",
            "last_refresh_at": now - timedelta(seconds=2),  # sibling just refreshed
        }

        vdb = MagicMock()
        vdb.get_oidc_session_by_id = MagicMock()
        vdb.get_oidc_session_by_id.remote = AsyncMock(return_value=fresh_row)
        vdb.update_oidc_session_tokens = MagicMock()
        vdb.update_oidc_session_tokens.remote = AsyncMock()

        # Sentinel: the IdP client must NOT be contacted.
        fake_client = MagicMock()
        fake_client.refresh_access_token = AsyncMock(
            side_effect=AssertionError("IdP must not be called during stampede short-circuit")
        )
        with patch.object(refresh_mod, "get_oidc_client", return_value=fake_client):
            out = await refresh_session_if_needed(session=stale_session, enc_key="k", vectordb=vdb)

        assert out is fresh_row
        fake_client.refresh_access_token.assert_not_awaited()
        vdb.update_oidc_session_tokens.remote.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_recovers_when_idp_rejects_stale_refresh_token(self):
        """IdP rejects our refresh_token (sibling already rotated it); the helper
        re-reads the session and returns the sibling's fresh tokens."""
        from components.auth import refresh as refresh_mod
        from components.auth.refresh import refresh_session_if_needed

        now = datetime.now()
        stale_session = {
            "id": 1,
            "access_token_expires_at": now + timedelta(seconds=5),
            "refresh_token_encrypted": b"old-refresh",
            # No recent last_refresh_at → stampede short-circuit does NOT fire.
            "last_refresh_at": None,
        }
        fresh_row = {
            "id": 1,
            "access_token_expires_at": now + timedelta(minutes=30),
            "refresh_token_encrypted": b"new-refresh",
            "access_token_encrypted": b"new-access",
            "last_refresh_at": now,
        }

        vdb = MagicMock()
        vdb.get_oidc_session_by_id = MagicMock()
        vdb.get_oidc_session_by_id.remote = AsyncMock(return_value=fresh_row)

        fake_client = MagicMock()
        fake_client.refresh_access_token = AsyncMock(side_effect=RuntimeError("invalid_grant"))
        with (
            patch.object(refresh_mod, "get_oidc_client", return_value=fake_client),
            patch.object(refresh_mod, "decrypt_token", return_value="old-refresh-plain"),
        ):
            out = await refresh_session_if_needed(session=stale_session, enc_key="k", vectordb=vdb)

        assert out is fresh_row
        fake_client.refresh_access_token.assert_awaited_once()
        vdb.get_oidc_session_by_id.remote.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_refresh_returns_none_when_idp_rejects_and_no_concurrent_refresh(self):
        """IdP rejects us and no sibling rotated the tokens → invalidate session."""
        from components.auth import refresh as refresh_mod
        from components.auth.refresh import refresh_session_if_needed

        now = datetime.now()
        stale_session = {
            "id": 1,
            "access_token_expires_at": now + timedelta(seconds=5),
            "refresh_token_encrypted": b"old-refresh",
            "last_refresh_at": None,
        }
        # Re-read returns the same stale row (no sibling rotation).
        stale_row_from_db = dict(stale_session)

        vdb = MagicMock()
        vdb.get_oidc_session_by_id = MagicMock()
        vdb.get_oidc_session_by_id.remote = AsyncMock(return_value=stale_row_from_db)

        fake_client = MagicMock()
        fake_client.refresh_access_token = AsyncMock(side_effect=RuntimeError("invalid_grant"))
        with (
            patch.object(refresh_mod, "get_oidc_client", return_value=fake_client),
            patch.object(refresh_mod, "decrypt_token", return_value="old-refresh-plain"),
        ):
            out = await refresh_session_if_needed(session=stale_session, enc_key="k", vectordb=vdb)

        assert out is None
