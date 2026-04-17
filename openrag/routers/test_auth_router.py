"""Integration tests for the OIDC auth router.

The router transitively imports ``utils.dependencies``, which spins up Ray
actors at import time (indexer, marker pool, semaphores, …). To avoid that
in a unit-test context, we stub ``utils.dependencies`` in ``sys.modules``
*before* importing the router, then drive it via FastAPI's ``TestClient``.

IdP interactions are mocked end-to-end with ``respx`` using a real RSA key
pair so the router exercises actual JWT verification.
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# pytest sometimes initialises warning filters before we run — tolerate it.
# ---------------------------------------------------------------------------

pytest.importorskip("respx")
pytest.importorskip("httpx")
pytest.importorskip("authlib")
pytest.importorskip("fastapi")
pytest.importorskip("itsdangerous")
pytest.importorskip("cryptography")

import httpx  # noqa: E402
import respx  # noqa: E402
from authlib.jose import JsonWebKey, JsonWebToken  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Constants — align with the existing auth unit tests
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


def _make_rsa_key_pair():
    private = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    return private, private.as_dict(is_private=True), private.as_dict()


_RSA_PRIVATE, _RSA_PRIVATE_JWK, _RSA_PUBLIC_JWK = _make_rsa_key_pair()
_RSA_PUBLIC_JWK["use"] = "sig"
_RSA_PUBLIC_JWK["alg"] = "RS256"
_RSA_PUBLIC_JWK.setdefault("kid", "test-key-1")
_RSA_PRIVATE_JWK.setdefault("kid", "test-key-1")

JWKS_RESPONSE = {"keys": [_RSA_PUBLIC_JWK]}


def _sign_jwt(payload: dict) -> str:
    header = {"alg": "RS256", "kid": "test-key-1"}
    jwt = JsonWebToken()
    token = jwt.encode(header, payload, _RSA_PRIVATE)
    return token.decode() if isinstance(token, bytes) else token


def _id_token_payload(
    nonce: str, *, sub: str = "sub-abc", email: str | None = "user@example.com", extra: dict | None = None
) -> dict:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": sub,
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": nonce,
    }
    if email is not None:
        payload["email"] = email
    if extra:
        payload.update(extra)
    return payload


def _logout_token_payload(*, sid: str | None = None, sub: str | None = None) -> dict:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "iat": now,
        "jti": "lt-001",
        "events": {"http://schemas.openid.net/event/backchannel-logout": {}},
    }
    if sid is not None:
        payload["sid"] = sid
    if sub is not None:
        payload["sub"] = sub
    return payload


# ---------------------------------------------------------------------------
# Stub heavy dependencies BEFORE importing the router
# ---------------------------------------------------------------------------

_FERNET_KEY = Fernet.generate_key().decode()


class _RayMethodStub:
    """Mimics a Ray actor method: ``method.remote(...)`` returns an awaitable."""

    def __init__(self, name: str, fn, call_log: list):
        self._name = name
        self._fn = fn
        self._call_log = call_log

    async def remote(self, *args, **kwargs):
        self._call_log.append((self._name, args, kwargs))
        return self._fn(*args, **kwargs)


class _StubVectorDB:
    """Minimal Ray-actor stand-in — exposes ``.method.remote(...)`` awaitables."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._users_by_sub: dict[str, dict] = {}
        self._users_by_id: dict[int, dict] = {}
        self._sessions: dict[int, dict] = {}
        self._sessions_by_token: dict[str, int] = {}
        self._next_session_id = 1
        # Bind each underlying impl as an actor-style accessor.
        self.get_user_by_external_id = _RayMethodStub(
            "get_user_by_external_id", self._impl_get_user_by_external_id, self.calls
        )
        self.update_user_fields = _RayMethodStub("update_user_fields", self._impl_update_user_fields, self.calls)
        self.create_oidc_session = _RayMethodStub("create_oidc_session", self._impl_create_oidc_session, self.calls)
        self.get_oidc_session_by_token = _RayMethodStub(
            "get_oidc_session_by_token", self._impl_get_oidc_session_by_token, self.calls
        )
        self.revoke_oidc_session_by_id = _RayMethodStub(
            "revoke_oidc_session_by_id", self._impl_revoke_oidc_session_by_id, self.calls
        )
        self.revoke_oidc_sessions_by_sid = _RayMethodStub(
            "revoke_oidc_sessions_by_sid", self._impl_revoke_oidc_sessions_by_sid, self.calls
        )

    # Test-only helpers -----------------------------------------------------

    def add_user(
        self,
        *,
        user_id: int,
        email: str | None = None,
        external_user_id: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        user = {
            "id": user_id,
            "email": email,
            "external_user_id": external_user_id,
            "is_admin": False,
            "display_name": display_name or f"user-{user_id}",
        }
        self._users_by_id[user_id] = user
        if external_user_id:
            self._users_by_sub[external_user_id] = user
        return user

    # Impls ------------------------------------------------------------------

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
        sid = self._next_session_id
        self._next_session_id += 1
        row = {
            "id": sid,
            "session_expires_at": kwargs["session_expires_at"],
            "id_token_encrypted": kwargs["id_token_encrypted"],
            **{k: v for k, v in kwargs.items() if k != "session_token_plain"},
        }
        self._sessions[sid] = row
        self._sessions_by_token[kwargs["session_token_plain"]] = sid
        return row

    def _impl_get_oidc_session_by_token(self, session_token_plain: str):
        # Mirror PartitionFileManager.get_oidc_session_by_token semantics:
        # reject revoked rows AND rows whose session_expires_at is in the past
        # relative to datetime.now(). The expiry check is what makes this stub
        # a faithful regression target for the M2 timezone fix.
        from datetime import datetime as _dt

        sid = self._sessions_by_token.get(session_token_plain)
        if sid is None:
            return None
        row = self._sessions[sid]
        if row.get("revoked_at"):
            return None
        exp = row.get("session_expires_at")
        if isinstance(exp, _dt) and exp < _dt.now():
            return None
        return row

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


_stub_vectordb_singleton = _StubVectorDB()


def _install_dependencies_stub():
    """Replace ``utils.dependencies`` with a stub providing only ``get_vectordb``."""
    stub = types.ModuleType("utils.dependencies")
    stub.get_vectordb = lambda: _stub_vectordb_singleton
    stub.get_task_state_manager = lambda: None
    stub.get_serializer = lambda: None
    stub.get_indexer = lambda: None
    stub.get_marker_pool = lambda: None
    sys.modules["utils.dependencies"] = stub


_install_dependencies_stub()


# Now we can import the router.
import importlib  # noqa: E402

# Reset the OIDC client singleton between tests — important when env changes.
from components.auth import deps as _auth_deps  # noqa: E402

# Import the router module, forcing a fresh import.
sys.modules.pop("routers.auth", None)
_auth_router_module = importlib.import_module("routers.auth")
auth_router = _auth_router_module.router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_oidc(monkeypatch):
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
    _auth_deps.reset_oidc_client()


@pytest.fixture
def env_token(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")
    _auth_deps.reset_oidc_client()


@pytest.fixture
def fresh_stub_vectordb():
    global _stub_vectordb_singleton
    # Re-create so tests see a clean state.
    _stub_vectordb_singleton.__init__()
    return _stub_vectordb_singleton


@pytest.fixture
def client(env_oidc, fresh_stub_vectordb):
    """TestClient for the minimal FastAPI app.

    The OIDCClient singleton uses a shared respx-mocked transport so every
    IdP route can be stubbed per-test via ``mock.router.get(...)``.
    """
    app = FastAPI()
    app.include_router(auth_router)

    # Replace the OIDCClient's internal httpx client with one backed by respx.
    transport = respx.MockTransport(assert_all_called=False)
    http = httpx.AsyncClient(transport=transport)

    # Force singleton creation using our mocked http client.
    _auth_deps.reset_oidc_client()
    _auth_deps._client = _auth_router_module.OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scopes=SCOPES,
        http_client=http,
    )

    c = TestClient(app)
    c.oidc_transport = transport  # type: ignore[attr-defined]
    yield c


def _setup_discovery(transport):
    transport.router.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=DISCOVERY_DOC)
    )


def _setup_jwks(transport):
    transport.router.get(f"{ISSUER}/protocol/openid-connect/certs").mock(
        return_value=httpx.Response(200, json=JWKS_RESPONSE)
    )


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------


def test_login_rejected_in_token_mode(env_token, fresh_stub_vectordb):
    app = FastAPI()
    app.include_router(auth_router)
    c = TestClient(app)
    r = c.get("/auth/login", follow_redirects=False)
    assert r.status_code == 400


def test_login_redirects_to_idp_with_pkce(client):
    _setup_discovery(client.oidc_transport)
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(f"{ISSUER}/protocol/openid-connect/auth")
    assert "code_challenge_method=S256" in loc
    assert "state=" in loc
    assert "nonce=" in loc
    assert "code_challenge=" in loc
    # State cookie set
    assert "openrag_oidc_state" in r.cookies


# ---------------------------------------------------------------------------
# GET /auth/callback — failure paths
# ---------------------------------------------------------------------------


def test_callback_rejected_in_token_mode(env_token, fresh_stub_vectordb):
    app = FastAPI()
    app.include_router(auth_router)
    c = TestClient(app)
    r = c.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 400


def test_callback_missing_state_cookie(client):
    r = client.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 400
    assert "state cookie" in r.json()["detail"].lower()


def test_callback_state_mismatch(client):
    _setup_discovery(client.oidc_transport)
    # First, obtain a legitimate state cookie via /auth/login.
    login_resp = client.get("/auth/login", follow_redirects=False)
    assert login_resp.status_code == 302

    # Now call /auth/callback with a *different* state in the query.
    r = client.get(
        "/auth/callback?code=x&state=WRONG",
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "state" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /auth/callback — success paths
# ---------------------------------------------------------------------------


def _begin_login_and_extract_state(client) -> tuple[str, str]:
    """Call /auth/login and return (state, nonce) values from the redirect query."""
    _setup_discovery(client.oidc_transport)
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(loc).query)
    return qs["state"][0], qs["nonce"][0]


def _mock_token_endpoint(transport, id_token: str, *, refresh_token: str | None = "rt-1"):
    payload = {
        "id_token": id_token,
        "access_token": "at-1",
        "expires_in": 300,
        "token_type": "Bearer",
    }
    if refresh_token is not None:
        payload["refresh_token"] = refresh_token
    transport.router.post(f"{ISSUER}/protocol/openid-connect/token").mock(
        return_value=httpx.Response(200, json=payload)
    )


def test_callback_success_by_external_id(client, fresh_stub_vectordb):
    fresh_stub_vectordb.add_user(user_id=42, email="user@example.com", external_user_id="sub-abc")
    _setup_jwks(client.oidc_transport)
    state, nonce = _begin_login_and_extract_state(client)
    id_token = _sign_jwt(_id_token_payload(nonce, sub="sub-abc"))
    _mock_token_endpoint(client.oidc_transport, id_token)

    r = client.get(
        f"/auth/callback?code=authcode&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "/"
    assert "openrag_session" in r.cookies
    # At least one create_oidc_session call recorded.
    assert any(c[0] == "create_oidc_session" for c in fresh_stub_vectordb.calls)


def test_callback_user_not_registered(client, fresh_stub_vectordb):
    """Unknown sub → 403 (no email fallback, no auto-provisioning)."""
    _setup_jwks(client.oidc_transport)
    state, nonce = _begin_login_and_extract_state(client)
    id_token = _sign_jwt(_id_token_payload(nonce, sub="sub-unknown", email="ghost@example.com"))
    _mock_token_endpoint(client.oidc_transport, id_token)

    r = client.get(
        f"/auth/callback?code=c&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "not registered" in r.json()["detail"].lower()


def test_callback_applies_claim_mapping_from_id_token(client, fresh_stub_vectordb, monkeypatch):
    """With OIDC_CLAIM_MAPPING set, claims from the ID token update the user row."""
    monkeypatch.setenv("OIDC_CLAIM_MAPPING", "display_name:name,email:email")
    monkeypatch.setenv("OIDC_CLAIM_SOURCE", "id_token")
    fresh_stub_vectordb.add_user(
        user_id=42,
        email="old@example.com",
        external_user_id="sub-abc",
        display_name="Old Name",
    )
    _setup_jwks(client.oidc_transport)
    state, nonce = _begin_login_and_extract_state(client)
    id_token = _sign_jwt(
        _id_token_payload(
            nonce,
            sub="sub-abc",
            email="dwho@badwolf.org",
            extra={"name": "Doctor Who"},
        )
    )
    _mock_token_endpoint(client.oidc_transport, id_token)

    r = client.get(
        f"/auth/callback?code=c&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    user = fresh_stub_vectordb._users_by_id[42]
    assert user["display_name"] == "Doctor Who"
    # email lowercased by update_user_fields stub
    assert user["email"] == "dwho@badwolf.org"
    # update_user_fields was called exactly once
    assert sum(1 for c in fresh_stub_vectordb.calls if c[0] == "update_user_fields") == 1


def test_callback_applies_claim_mapping_from_userinfo(client, fresh_stub_vectordb, monkeypatch):
    """With OIDC_CLAIM_SOURCE=userinfo the claim fetch goes to /userinfo."""
    monkeypatch.setenv("OIDC_CLAIM_MAPPING", "display_name:name,email:email")
    monkeypatch.setenv("OIDC_CLAIM_SOURCE", "userinfo")
    fresh_stub_vectordb.add_user(
        user_id=55,
        email=None,
        external_user_id="sub-ui",
        display_name="legacy",
    )
    _setup_jwks(client.oidc_transport)
    state, nonce = _begin_login_and_extract_state(client)
    # ID token carries no name/email — the router must pull them from /userinfo.
    id_token = _sign_jwt(_id_token_payload(nonce, sub="sub-ui", email=None))
    _mock_token_endpoint(client.oidc_transport, id_token)
    userinfo_route = client.oidc_transport.router.get(f"{ISSUER}/protocol/openid-connect/userinfo").mock(
        return_value=httpx.Response(
            200,
            json={"sub": "sub-ui", "name": "UI User", "email": "ui@example.com"},
        )
    )

    r = client.get(
        f"/auth/callback?code=c&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert userinfo_route.called
    user = fresh_stub_vectordb._users_by_id[55]
    assert user["display_name"] == "UI User"
    assert user["email"] == "ui@example.com"


def test_callback_skips_mapping_when_unset(client, fresh_stub_vectordb, monkeypatch):
    """Without OIDC_CLAIM_MAPPING the user row is not touched."""
    monkeypatch.delenv("OIDC_CLAIM_MAPPING", raising=False)
    fresh_stub_vectordb.add_user(
        user_id=77,
        email="tester@example.com",
        external_user_id="sub-plain",
        display_name="Initial",
    )
    _setup_jwks(client.oidc_transport)
    state, nonce = _begin_login_and_extract_state(client)
    id_token = _sign_jwt(
        _id_token_payload(
            nonce,
            sub="sub-plain",
            email="different@example.com",
            extra={"name": "Should Be Ignored"},
        )
    )
    _mock_token_endpoint(client.oidc_transport, id_token)

    r = client.get(
        f"/auth/callback?code=c&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    user = fresh_stub_vectordb._users_by_id[77]
    # Untouched by the callback when OIDC_CLAIM_MAPPING is empty
    assert user["display_name"] == "Initial"
    assert user["email"] == "tester@example.com"
    # update_user_fields was never called
    assert not any(c[0] == "update_user_fields" for c in fresh_stub_vectordb.calls)


# ---------------------------------------------------------------------------
# POST /auth/backchannel-logout
# ---------------------------------------------------------------------------


def test_backchannel_logout_rejects_invalid_token(client):
    _setup_discovery(client.oidc_transport)
    _setup_jwks(client.oidc_transport)
    r = client.post(
        "/auth/backchannel-logout",
        data={"logout_token": "not-a-jwt"},
    )
    assert r.status_code == 400


def test_backchannel_logout_revokes_by_sid(client, fresh_stub_vectordb):
    _setup_jwks(client.oidc_transport)
    _setup_discovery(client.oidc_transport)

    # Seed a session to be revoked
    fresh_stub_vectordb._sessions[1] = {
        "id": 1,
        "sid": "sid-target",
        "revoked_at": None,
    }
    token = _sign_jwt(_logout_token_payload(sid="sid-target"))
    r = client.post(
        "/auth/backchannel-logout",
        data={"logout_token": token},
    )
    assert r.status_code == 200
    # The stub increments revoked_at on matching sid
    assert fresh_stub_vectordb._sessions[1]["revoked_at"] is not None


# ---------------------------------------------------------------------------
# GET /auth/logout
# ---------------------------------------------------------------------------


def test_logout_revokes_session_and_deletes_cookie(client, fresh_stub_vectordb):
    _setup_discovery(client.oidc_transport)
    # Seed a session & cookie
    session_token = "sess-logout-tok"
    fresh_stub_vectordb._sessions[1] = {
        "id": 1,
        "sid": "sid-1",
        "id_token_encrypted": None,  # skip decrypt path
        "session_expires_at": time.time() + 3600,
        "revoked_at": None,
    }
    fresh_stub_vectordb._sessions_by_token[session_token] = 1

    r = client.get(
        "/auth/logout",
        cookies={"openrag_session": session_token},
        follow_redirects=False,
    )
    assert r.status_code == 302
    # Session marked revoked
    assert fresh_stub_vectordb._sessions[1]["revoked_at"] is not None
    # Cookie cleared in response (max-age=0 or Expires=past)
    set_cookie_headers = r.headers.get_list("set-cookie")
    assert any("openrag_session=" in h and ("Max-Age=0" in h or "expires=" in h.lower()) for h in set_cookie_headers)


def test_logout_rejected_in_token_mode(env_token, fresh_stub_vectordb):
    app = FastAPI()
    app.include_router(auth_router)
    c = TestClient(app)
    r = c.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Skips — scenarios we can add once the full middleware stack is wired (phase 5)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="requires phase-5 middleware for cookie-based auth on /auth/me")
def test_me_returns_user_info_with_valid_cookie():
    pass


# ---------------------------------------------------------------------------
# M2: timezone-consistency regression test
#
# Before the fix, routers/auth.py wrote ``access_token_expires_at`` /
# ``session_expires_at`` via ``datetime.utcnow()`` while every read site
# compared against ``datetime.now()``. On a host whose TZ is east of UTC
# (e.g. Europe/Paris), a newly-issued session thus appeared "already
# expired" by tz_offset hours and ``get_oidc_session_by_token`` returned
# None immediately after the callback.
# ---------------------------------------------------------------------------


def test_callback_session_not_prematurely_expired_under_nonutc_tz(client, fresh_stub_vectordb, monkeypatch):
    """Callback in a non-UTC timezone must produce an immediately usable session."""
    import os as _os

    # Force a non-UTC timezone for the duration of this test. If the platform
    # doesn't support ``time.tzset`` (e.g. Windows CI runners), skip gracefully.
    tzset = getattr(time, "tzset", None)
    if tzset is None:
        pytest.skip("time.tzset not available on this platform; cannot force TZ")

    original_tz = _os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Paris")
    tzset()
    try:
        # Teach the stub to back get_oidc_session_by_token with the same dict we
        # created in create_oidc_session (the default stub already does).
        fresh_stub_vectordb.add_user(user_id=77, email="tz@example.com", external_user_id="sub-tz")
        _setup_jwks(client.oidc_transport)
        state, nonce = _begin_login_and_extract_state(client)
        id_token = _sign_jwt(_id_token_payload(nonce, sub="sub-tz", email="tz@example.com"))
        _mock_token_endpoint(client.oidc_transport, id_token)

        r = client.get(
            f"/auth/callback?code=c&state={state}",
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text

        # Pull the session cookie value from the response and look it up via
        # the stub — this exercises the same staleness comparison the real
        # middleware uses at request time.
        session_cookie = r.cookies.get("openrag_session")
        assert session_cookie, "callback did not set openrag_session cookie"

        fetched = fresh_stub_vectordb._impl_get_oidc_session_by_token(session_cookie)
        assert fetched is not None, "Session appeared expired IMMEDIATELY after creation — tz bug (M2)"

        # Additional sanity: session_expires_at must be strictly in the future
        # from the perspective of datetime.now() (the read-site clock).
        from datetime import datetime as _dt

        session_exp = fetched["session_expires_at"]
        assert session_exp > _dt.now(), f"session_expires_at={session_exp} is not in the future vs datetime.now()"
    finally:
        if original_tz is None:
            _os.environ.pop("TZ", None)
        else:
            _os.environ["TZ"] = original_tz
        tzset()
