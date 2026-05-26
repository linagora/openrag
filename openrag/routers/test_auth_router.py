"""Transport tests for the thin OIDC auth router (Phase 8A.1).

Every OIDC business decision (PKCE/state generation, code exchange, user
lookup/provisioning, session creation, logout-URL construction, JWT
verification) now lives in :class:`services.orchestrators.auth_service.
AuthService` and is covered end-to-end by
``services/orchestrators/test_auth_service.py``.

This module only asserts what the router still owns: the ``AUTH_MODE``
gate, delegation to the injected service, cookie set/clear, and the
``OIDCFlowError`` → HTTP-response mapping. The service is stubbed via
``dependency_overrides`` so no container / Ray / IdP is needed.

The router transitively imports ``services.workers.bootstrap`` (Ray actors
at import time) through its dependency graph, so we stub that module in
``sys.modules`` before importing the router.
"""

from __future__ import annotations

import sys
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Stub heavy dependencies BEFORE importing the router
# ---------------------------------------------------------------------------


_STUBBED_MODULES = ("utils", "utils.logger", "services.workers.bootstrap")


def _install_dependencies_stub() -> dict[str, types.ModuleType | None]:
    previous_modules = {name: sys.modules.get(name) for name in _STUBBED_MODULES}

    stub = types.ModuleType("services.workers.bootstrap")
    stub.actor_creation_map = {}
    stub.get_task_state_manager = lambda: None
    stub.get_serializer = lambda: None
    stub.get_marker_pool = lambda: None
    sys.modules["services.workers.bootstrap"] = stub

    def _logger():
        logger = types.SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        )
        logger.bind = lambda *args, **kwargs: logger
        return logger

    logger_stub = types.ModuleType("utils.logger")
    logger_stub.escape_markup = lambda s: s.replace("\\", "\\\\").replace("<", "\\<").replace(">", "\\>")
    logger_stub.mask_email = (
        lambda email: f"{email.partition('@')[0][0]}***@{email.partition('@')[2]}"
        if isinstance(email, str) and "@" in email and email.partition("@")[0]
        else "***"
    )
    logger_stub.get_logger = _logger
    sys.modules["utils.logger"] = logger_stub
    return previous_modules


def _restore_dependencies_stub(previous_modules: dict[str, types.ModuleType | None]) -> None:
    for name, previous_module in previous_modules.items():
        if previous_module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous_module


_PREVIOUS_MODULES = _install_dependencies_stub()

from di.providers import get_auth_service  # noqa: E402
from routers.auth import router as auth_router  # noqa: E402
from services.orchestrators.auth_service import (  # noqa: E402
    SESSION_COOKIE_NAME,
    CallbackResult,
    LoginRedirect,
    OIDCFlowError,
)

_restore_dependencies_stub(_PREVIOUS_MODULES)

STATE_COOKIE_NAME = "openrag_oidc_state"


# ---------------------------------------------------------------------------
# Stub AuthService
# ---------------------------------------------------------------------------


class StubAuthService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.login_result = LoginRedirect(
            authorization_url="https://idp.example.com/auth?response_type=code",
            state_cookie_name=STATE_COOKIE_NAME,
            state_cookie_value="state-val",
            state_cookie_max_age=600,
        )
        self.callback_result = CallbackResult(
            session_cookie_name=SESSION_COOKIE_NAME,
            session_cookie_value="sess-plain",
            session_cookie_max_age=300,
            next_url="/next",
        )
        self.logout_target: str | None = "https://idp.example.com/logout"
        self.raise_on: dict[str, OIDCFlowError] = {}

    async def start_oidc_login(self, next_url):
        self.calls.append(("start_oidc_login", next_url))
        if "login" in self.raise_on:
            raise self.raise_on["login"]
        return self.login_result

    async def handle_oidc_callback(self, *, code, state, state_cookie_raw):
        self.calls.append(("handle_oidc_callback", code, state, state_cookie_raw))
        if "callback" in self.raise_on:
            raise self.raise_on["callback"]
        return self.callback_result

    async def handle_backchannel_logout(self, logout_token):
        self.calls.append(("handle_backchannel_logout", logout_token))
        if "bcl" in self.raise_on:
            raise self.raise_on["bcl"]
        return 1

    async def logout(self, session_cookie_value):
        self.calls.append(("logout", session_cookie_value))
        return self.logout_target


def _set_cookies(response) -> list[str]:
    return [v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"]


@pytest.fixture
def stub() -> StubAuthService:
    return StubAuthService()


@pytest.fixture
def client(stub: StubAuthService) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_auth_service] = lambda: stub
    return TestClient(app)


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")


# ---------------------------------------------------------------------------
# AUTH_MODE gate — token mode must 400 *before* the service is resolved.
# Regression: previously these returned 503 because Depends(get_auth_service)
# resolved (and failed on a missing container) before the gate ran.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/auth/login", {}),
        ("get", "/auth/callback?code=x&state=y", {}),
        ("post", "/auth/backchannel-logout", {"data": {"logout_token": "x"}}),
        ("get", "/auth/logout", {}),
    ],
)
def test_routes_rejected_in_token_mode(token_env, method, path, kwargs):
    """No service override and no container — must still be a clean 400."""
    app = FastAPI()
    app.include_router(auth_router)
    c = TestClient(app)
    r = getattr(c, method)(path, follow_redirects=False, **kwargs)
    assert r.status_code == 400
    assert "AUTH_MODE" in r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------


def test_login_redirects_and_sets_state_cookie(oidc_env, client, stub):
    r = client.get("/auth/login?next=/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == stub.login_result.authorization_url
    assert stub.calls == [("start_oidc_login", "/dashboard")]
    assert any(STATE_COOKIE_NAME in c for c in _set_cookies(r))


def test_login_flow_error_maps_to_http(oidc_env, client, stub):
    stub.raise_on["login"] = OIDCFlowError("boom", status_code=502)
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 502
    assert r.json()["detail"] == "boom"


# ---------------------------------------------------------------------------
# GET /auth/callback
# ---------------------------------------------------------------------------


def test_callback_success_sets_session_clears_state(oidc_env, client, stub):
    r = client.get(
        "/auth/callback?code=ac&state=st",
        cookies={STATE_COOKIE_NAME: "raw-state"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/next"
    assert stub.calls == [("handle_oidc_callback", "ac", "st", "raw-state")]
    cookies = _set_cookies(r)
    assert any(SESSION_COOKIE_NAME in c for c in cookies)
    # State cookie is cleared (deletion emits a Set-Cookie with Max-Age=0).
    assert any(STATE_COOKIE_NAME in c and ("Max-Age=0" in c or "expires=" in c.lower()) for c in cookies)


def test_callback_flow_error_returns_json_and_clears_state(oidc_env, client, stub):
    stub.raise_on["callback"] = OIDCFlowError("bad state", status_code=400)
    r = client.get("/auth/callback?code=c&state=s", follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["detail"] == "bad state"
    assert any(STATE_COOKIE_NAME in c for c in _set_cookies(r))


# ---------------------------------------------------------------------------
# POST /auth/backchannel-logout
# ---------------------------------------------------------------------------


def test_backchannel_logout_success(oidc_env, client, stub):
    r = client.post("/auth/backchannel-logout", data={"logout_token": "lt"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert stub.calls == [("handle_backchannel_logout", "lt")]


def test_backchannel_logout_error_emits_invalid_request(oidc_env, client, stub):
    stub.raise_on["bcl"] = OIDCFlowError("nope", error_description="token expired")
    r = client.post("/auth/backchannel-logout", data={"logout_token": "lt"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "invalid_request"
    assert body["error_description"] == "token expired"
    assert r.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# GET /auth/logout
# ---------------------------------------------------------------------------


def test_logout_redirects_to_idp_and_clears_session(oidc_env, client, stub):
    r = client.get(
        "/auth/logout",
        cookies={SESSION_COOKIE_NAME: "sess"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == stub.logout_target
    assert stub.calls == [("logout", "sess")]
    assert any(SESSION_COOKIE_NAME in c and ("Max-Age=0" in c or "expires=" in c.lower()) for c in _set_cookies(r))


def test_logout_without_idp_target_confirms_in_place(oidc_env, client, stub):
    stub.logout_target = None
    r = client.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 200
    assert r.json()["detail"] == "Logged out"
    assert any(SESSION_COOKIE_NAME in c for c in _set_cookies(r))
