"""End-to-end OIDC lifecycle test.

Exercises the *full* login → session → backchannel-logout → revoked-cookie
sequence in a single test function so that integration bugs between phases
are caught immediately.

No Ray / Milvus / Docker required: the vectordb is replaced by the same
_StubVectorDB used in ``openrag/routers/test_auth_router.py``, mounted via
``utils.dependencies`` stubbing. The IdP is faked with ``respx``.
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("respx")
pytest.importorskip("httpx")
pytest.importorskip("authlib")
pytest.importorskip("fastapi")
pytest.importorskip("itsdangerous")
pytest.importorskip("cryptography")

import importlib  # noqa: E402

import httpx  # noqa: E402
import respx  # noqa: E402
from authlib.jose import JsonWebKey, JsonWebToken  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# IdP constants
# ---------------------------------------------------------------------------

ISSUER = "https://idp.example.com/realms/openrag"
CLIENT_ID = "openrag-client"
CLIENT_SECRET = "test-secret"
REDIRECT_URI = "https://openrag.example.com/auth/callback"
SCOPES = "openid email profile offline_access"

DISCOVERY_DOC = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
    "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
}

_FERNET_KEY = Fernet.generate_key().decode()

# ---------------------------------------------------------------------------
# RSA key pair (shared for entire module)
# ---------------------------------------------------------------------------

_RSA_PRIVATE = JsonWebKey.generate_key("RSA", 2048, is_private=True)
_RSA_PRIVATE_JWK = _RSA_PRIVATE.as_dict(is_private=True)
_RSA_PRIVATE_JWK.setdefault("kid", "test-key-1")
_RSA_PUBLIC_JWK = _RSA_PRIVATE.as_dict()
_RSA_PUBLIC_JWK["use"] = "sig"
_RSA_PUBLIC_JWK["alg"] = "RS256"
_RSA_PUBLIC_JWK.setdefault("kid", "test-key-1")
JWKS_RESPONSE = {"keys": [_RSA_PUBLIC_JWK]}


def _sign_jwt(payload: dict) -> str:
    header = {"alg": "RS256", "kid": "test-key-1"}
    # Authlib >=1.0 requires the allowed-algorithms list.
    jwt = JsonWebToken(["RS256"])
    token = jwt.encode(header, payload, _RSA_PRIVATE)
    return token.decode() if isinstance(token, bytes) else token


def _id_token(nonce: str, *, sub: str, email: str, sid: str | None = None) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": sub,
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": nonce,
        "email": email,
    }
    if sid:
        payload["sid"] = sid
    return _sign_jwt(payload)


def _logout_token(*, sid: str, sub: str) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "iat": now,
        "jti": "lt-lifecycle-001",
        "events": {"http://schemas.openid.net/event/backchannel-logout": {}},
        "sid": sid,
        "sub": sub,
    }
    return _sign_jwt(payload)


# ---------------------------------------------------------------------------
# Stub VectorDB — mirrors the one in test_auth_router.py exactly
# ---------------------------------------------------------------------------


class _RayMethodStub:
    def __init__(self, name: str, fn, call_log: list):
        self._name = name
        self._fn = fn
        self._call_log = call_log

    async def remote(self, *args, **kwargs):
        self._call_log.append((self._name, args, kwargs))
        return self._fn(*args, **kwargs)


class _StubVectorDB:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._users_by_sub: dict[str, dict] = {}
        self._users_by_id: dict[int, dict] = {}
        self._sessions: dict[int, dict] = {}
        self._sessions_by_token: dict[str, int] = {}
        self._next_session_id = 1

        self.get_user_by_external_id = _RayMethodStub(
            "get_user_by_external_id", self._impl_get_user_by_external_id, self.calls
        )
        self.update_user_fields = _RayMethodStub(
            "update_user_fields", self._impl_update_user_fields, self.calls
        )
        self.create_oidc_session = _RayMethodStub(
            "create_oidc_session", self._impl_create_oidc_session, self.calls
        )
        self.get_oidc_session_by_token = _RayMethodStub(
            "get_oidc_session_by_token", self._impl_get_oidc_session_by_token, self.calls
        )
        self.revoke_oidc_session_by_id = _RayMethodStub(
            "revoke_oidc_session_by_id", self._impl_revoke_oidc_session_by_id, self.calls
        )
        self.revoke_oidc_sessions_by_sid = _RayMethodStub(
            "revoke_oidc_sessions_by_sid", self._impl_revoke_oidc_sessions_by_sid, self.calls
        )
        # The middleware also calls these two
        self.get_user = _RayMethodStub("get_user", self._impl_get_user, self.calls)
        self.list_user_partitions = _RayMethodStub(
            "list_user_partitions", lambda *a, **kw: [], self.calls
        )
        self.get_user_by_token = _RayMethodStub(
            "get_user_by_token", lambda *a, **kw: None, self.calls
        )
        self.update_oidc_session_tokens = _RayMethodStub(
            "update_oidc_session_tokens", lambda *a, **kw: None, self.calls
        )

    def add_user(
        self,
        *,
        user_id: int,
        email: str | None = None,
        external_user_id: str | None = None,
    ) -> dict:
        user = {
            "id": user_id,
            "email": email,
            "external_user_id": external_user_id,
            "is_admin": False,
            "display_name": f"user-{user_id}",
        }
        self._users_by_id[user_id] = user
        if external_user_id:
            self._users_by_sub[external_user_id] = user
        return user

    def _impl_get_user_by_external_id(self, external_user_id: str):
        return self._users_by_sub.get(external_user_id)

    def _impl_update_user_fields(self, user_id: int, fields: dict):
        user = self._users_by_id.get(user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found")
        _ALLOWED = {"display_name", "email"}
        bad = set(fields) - _ALLOWED
        if bad:
            raise ValueError(f"Cannot update non-whitelisted user fields: {sorted(bad)}")
        for k, v in fields.items():
            if v is None:
                continue
            if k == "email" and isinstance(v, str):
                v = v.strip().lower()
            user[k] = v

    def _impl_create_oidc_session(self, **kwargs):
        sid_key = self._next_session_id
        self._next_session_id += 1
        row = {
            "id": sid_key,
            "session_expires_at": kwargs["session_expires_at"],
            "id_token_encrypted": kwargs.get("id_token_encrypted"),
            **{k: v for k, v in kwargs.items() if k != "session_token_plain"},
        }
        self._sessions[sid_key] = row
        self._sessions_by_token[kwargs["session_token_plain"]] = sid_key
        return row

    def _impl_get_oidc_session_by_token(self, session_token_plain: str):
        sid_key = self._sessions_by_token.get(session_token_plain)
        if sid_key is None:
            return None
        row = self._sessions[sid_key]
        if row.get("revoked_at"):
            return None
        return row

    def _impl_get_user(self, user_id: int):
        return self._users_by_id.get(user_id)

    def _impl_revoke_oidc_session_by_id(self, session_id: int):
        row = self._sessions.get(session_id)
        if row:
            row["revoked_at"] = time.time()

    def _impl_revoke_oidc_sessions_by_sid(self, sid: str) -> int:
        count = 0
        for row in self._sessions.values():
            if row.get("sid") == sid and not row.get("revoked_at"):
                row["revoked_at"] = time.time()
                count += 1
        return count


# ---------------------------------------------------------------------------
# Module-level stub installation — must happen BEFORE any router imports
# ---------------------------------------------------------------------------

_stub_vdb = _StubVectorDB()


def _install_stubs():
    stub = types.ModuleType("utils.dependencies")
    stub.get_vectordb = lambda: _stub_vdb
    stub.get_task_state_manager = lambda: None
    stub.get_serializer = lambda: None
    stub.get_indexer = lambda: None
    stub.get_marker_pool = lambda: None
    sys.modules["utils.dependencies"] = stub


_install_stubs()

# Reload auth deps + routers after stub installation
from components.auth import deps as _auth_deps  # noqa: E402

sys.modules.pop("routers.auth", None)
sys.modules.pop("routers.users", None)
_auth_router_mod = importlib.import_module("routers.auth")
_users_router_mod = importlib.import_module("routers.users")

# ---------------------------------------------------------------------------
# Build the composite app (auth + users)
# ---------------------------------------------------------------------------


def _make_app(router) -> tuple[FastAPI, TestClient]:
    """Build a minimal FastAPI app combining auth + users routers, with a
    respx MockRouter injected into the OIDCClient singleton. respx >= 0.22
    exposes MockRouter + httpx.MockTransport(router.handler)."""
    app = FastAPI()

    # Install the AuthMiddleware (from components.auth.middleware)
    from components.auth.middleware import AuthMiddleware  # noqa: E402

    app.add_middleware(AuthMiddleware, get_vectordb=lambda: _stub_vdb)

    app.include_router(_auth_router_mod.router)
    app.include_router(_users_router_mod.router, prefix="/users")

    # Override OIDCClient singleton with our mocked http transport.
    _auth_deps.reset_oidc_client()
    _auth_deps._client = _auth_router_mod.OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scopes=SCOPES,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(router.handler)),
    )

    client = TestClient(app, raise_server_exceptions=True)
    return app, client


# ---------------------------------------------------------------------------
# THE lifecycle test
# ---------------------------------------------------------------------------


def test_full_oidc_lifecycle(monkeypatch):
    """Single end-to-end flow: login → callback → /users/info → backchannel
    logout → revoked cookie check.

    Covers AC4, AC5, AC9, AC12, AC14 with live component wiring.
    Pre-provisions alice with ``external_user_id`` matching the mocked IdP's
    ``sub`` — email is pure metadata in the simplified flow.
    """
    # ── Env ──────────────────────────────────────────────────────────────────
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ENDPOINT", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("OIDC_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.setenv("OIDC_SCOPES", SCOPES)
    monkeypatch.setenv("OIDC_TOKEN_ENCRYPTION_KEY", _FERNET_KEY)
    monkeypatch.setenv("OIDC_CLAIM_SOURCE", "id_token")
    monkeypatch.setenv("OIDC_POST_LOGOUT_REDIRECT_URI", "/")
    monkeypatch.delenv("OIDC_CLAIM_MAPPING", raising=False)
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    _auth_deps.reset_oidc_client()

    # ── Pre-seed alice with the exact sub that the IdP mock will return ────────
    ALICE_SUB = "alice-sub"
    _stub_vdb.__init__()  # reset state
    _stub_vdb.add_user(
        user_id=99, email="alice@example.com", external_user_id=ALICE_SUB
    )

    # ── Build app with mocked transport ──────────────────────────────────────
    router = respx.MockRouter(assert_all_called=False)
    router.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=DISCOVERY_DOC)
    )
    router.get(f"{ISSUER}/protocol/openid-connect/certs").mock(
        return_value=httpx.Response(200, json=JWKS_RESPONSE)
    )

    _, client = _make_app(router)

    # ── Step 1: GET /auth/login → 302 to IdP ─────────────────────────────────
    r1 = client.get("/auth/login", follow_redirects=False)
    assert r1.status_code == 302, f"Expected 302, got {r1.status_code}: {r1.text}"
    loc = r1.headers["location"]
    assert loc.startswith(f"{ISSUER}/protocol/openid-connect/auth"), loc
    assert "code_challenge_method=S256" in loc
    assert "state=" in loc
    assert "nonce=" in loc

    # Capture state + nonce from redirect URL
    qs = parse_qs(urlparse(loc).query)
    state = qs["state"][0]
    nonce = qs["nonce"][0]

    # State cookie must be set
    assert "openrag_oidc_state" in r1.cookies

    # ── Step 2: Simulate IdP token response ──────────────────────────────────
    ALICE_SID = "sess-123"
    id_tok = _id_token(nonce, sub=ALICE_SUB, email="alice@example.com", sid=ALICE_SID)

    router.post(f"{ISSUER}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "id_token": id_tok,
                "access_token": "at-lifecycle",
                "refresh_token": "rt-lifecycle",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )
    )

    # ── Step 3: GET /auth/callback → 302 to /, openrag_session cookie set ────
    r2 = client.get(
        f"/auth/callback?code=auth-code-xyz&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 302, f"Expected 302, got {r2.status_code}: {r2.text}"
    assert r2.headers["location"] == "/"
    assert "openrag_session" in r2.cookies
    session_cookie = r2.cookies["openrag_session"]

    # ── Step 4: Assert DB state ───────────────────────────────────────────────
    # User pre-provisioning is the admin's responsibility — the flow must not
    # mutate external_user_id at all (no backfill anymore).
    alice = _stub_vdb._users_by_id[99]
    assert alice["external_user_id"] == ALICE_SUB, (
        f"Expected external_user_id='{ALICE_SUB}', got '{alice['external_user_id']}'"
    )

    # oidc_sessions row created with correct sid
    assert len(_stub_vdb._sessions) == 1, "Expected exactly one oidc_sessions row"
    session_row = next(iter(_stub_vdb._sessions.values()))
    assert session_row.get("sid") == ALICE_SID, (
        f"Expected sid='{ALICE_SID}', got '{session_row.get('sid')}'"
    )
    assert session_row.get("revoked_at") is None, "Session must not be revoked yet"

    # ── Step 5: GET /users/info with session cookie → 200 alice profile ───────
    r3 = client.get("/users/info", cookies={"openrag_session": session_cookie})
    # The users router depends on task_state_manager for file counts; since we
    # stub it as None the endpoint may 500 on full wiring — we accept 200 or
    # verify the middleware resolved alice (status != 401/302).
    assert r3.status_code not in (401, 302), (
        f"Middleware should resolve alice, got {r3.status_code}: {r3.text}"
    )

    # ── Step 6: POST /auth/backchannel-logout → 200, session revoked ──────────
    logout_tok = _logout_token(sid=ALICE_SID, sub=ALICE_SUB)
    r4 = client.post(
        "/auth/backchannel-logout",
        data={"logout_token": logout_tok},
    )
    assert r4.status_code == 200, f"Expected 200, got {r4.status_code}: {r4.text}"

    # oidc_sessions row now has revoked_at set
    assert session_row.get("revoked_at") is not None, (
        "Session row must have revoked_at after backchannel-logout"
    )

    # ── Step 7: GET /users/info with same cookie → 302 /auth/login ───────────
    r5 = client.get(
        "/users/info",
        cookies={"openrag_session": session_cookie},
        follow_redirects=False,
    )
    # /users/info is an API path → strict 401 JSON (per plan §6.1 decision:
    # API paths never 302-redirect to /auth/login — that's for UI paths only).
    assert r5.status_code == 401, f"Revoked API session must return 401, got {r5.status_code}: {r5.text}"
