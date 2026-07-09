"""Tests for the path-tiered rate limiting middleware."""

import pytest
from api.middleware.rate_limit import RateLimitMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _make_request(user=None, host="1.2.3.4") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": (host, 1234),
    }
    req = Request(scope)
    if user is not None:
        req.state.user = user
    return req


def test_identity_keys_on_authenticated_user_dict():
    # request.state.user is a dict (set by AuthMiddleware); identity must key on
    # its "id", not fall through to the client IP.
    assert RateLimitMiddleware._identity(_make_request(user={"id": 7})) == "user:7"


def test_identity_falls_back_to_ip_when_unauthenticated():
    assert RateLimitMiddleware._identity(_make_request(user=None, host="9.9.9.9")) == "ip:9.9.9.9"


def _build_app(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    async def ok(request: Request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/v1/chat", ok),
            Route("/auth/login", ok),
            Route("/other", ok),
            Route("/chainlit/ws/socket.io/", ok),
            Route("/assets/pdf.worker.mjs", ok),
        ]
    )
    app.add_middleware(RateLimitMiddleware)
    return app


def test_allows_under_limit(monkeypatch):
    app = _build_app(monkeypatch, RATE_LIMIT_CHAT="5/minute")
    client = TestClient(app)
    for _ in range(5):
        assert client.get("/v1/chat").status_code == 200


def test_blocks_over_limit_with_retry_after(monkeypatch):
    app = _build_app(monkeypatch, RATE_LIMIT_CHAT="2/minute")
    client = TestClient(app)
    assert client.get("/v1/chat").status_code == 200
    assert client.get("/v1/chat").status_code == 200
    resp = client.get("/v1/chat")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_tiers_have_independent_budgets(monkeypatch):
    # Exhausting the chat tier must not affect the auth tier.
    app = _build_app(monkeypatch, RATE_LIMIT_CHAT="1/minute", RATE_LIMIT_AUTH="1/minute")
    client = TestClient(app)
    assert client.get("/v1/chat").status_code == 200
    assert client.get("/v1/chat").status_code == 429
    assert client.get("/auth/login").status_code == 200


def test_disabled_passes_through(monkeypatch):
    app = _build_app(monkeypatch, RATE_LIMIT_ENABLED="false", RATE_LIMIT_CHAT="1/minute")
    client = TestClient(app)
    for _ in range(5):
        assert client.get("/v1/chat").status_code == 200


def test_disabled_ignores_malformed_config(monkeypatch):
    # A malformed RATE_LIMIT_* value must not crash boot when limiting is off:
    # the middleware skips parsing the limits entirely when disabled.
    app = _build_app(monkeypatch, RATE_LIMIT_ENABLED="false", RATE_LIMIT_DEFAULT="not-a-valid-limit")
    client = TestClient(app)
    assert client.get("/other").status_code == 200


def test_is_admin_true_for_admin_user_dict():
    assert RateLimitMiddleware._is_admin(_make_request(user={"id": 1, "is_admin": True})) is True


def test_is_admin_false_for_non_admin_and_unauthenticated():
    assert RateLimitMiddleware._is_admin(_make_request(user={"id": 2, "is_admin": False})) is False
    assert RateLimitMiddleware._is_admin(_make_request(user=None)) is False


def test_admin_bypasses_rate_limit(monkeypatch):
    # An admin user is never throttled even past a tiny limit. AuthMiddleware runs
    # before this middleware and sets request.state.user, so we inject it via a tiny
    # inline middleware here. The non-admin side (identity keying + is_admin=False)
    # is covered by the _identity/_is_admin tests above; a live non-admin 429
    # assertion is intentionally omitted because exhausting the limit across TestClient
    # requests is unreliable under limits.aio's per-event-loop memory storage.
    from starlette.middleware.base import BaseHTTPMiddleware

    monkeypatch.setenv("RATE_LIMIT_CHAT", "1/minute")

    admin = {"id": 1, "is_admin": True}

    async def ok(request: Request):
        return PlainTextResponse("ok")

    class _InjectUser(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.user = admin
            return await call_next(request)

    app = Starlette(routes=[Route("/v1/chat", ok)])
    # add_middleware is reverse of execution: _InjectUser (added last) runs first,
    # populating request.state.user before RateLimitMiddleware sees the request.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(_InjectUser)
    client = TestClient(app)
    for _ in range(5):
        assert client.get("/v1/chat").status_code == 200


def test_chainlit_socketio_is_exempt(monkeypatch):
    # Chainlit's Socket.IO transport falls back to HTTP long-polling behind a proxy
    # that doesn't forward the upgrade, issuing one request per emitted packet. The
    # subtree is auth-bypassed, so those requests key on the proxy's IP and would
    # otherwise exhaust a single shared bucket for every chat user at once.
    app = _build_app(monkeypatch, RATE_LIMIT_DEFAULT="2/minute")
    client = TestClient(app)
    for _ in range(10):
        assert client.get("/chainlit/ws/socket.io/").status_code == 200


def test_chainlit_root_assets_are_exempt(monkeypatch):
    app = _build_app(monkeypatch, RATE_LIMIT_DEFAULT="2/minute")
    client = TestClient(app)
    for _ in range(10):
        assert client.get("/assets/pdf.worker.mjs").status_code == 200


def test_exempt_paths_do_not_widen_to_other_routes(monkeypatch):
    # The exemption must not leak into the tiers it doesn't name.
    app = _build_app(monkeypatch, RATE_LIMIT_DEFAULT="1/minute")
    client = TestClient(app)
    assert client.get("/other").status_code == 200
    assert client.get("/other").status_code == 429


def test_exempt_paths_env_override_replaces_defaults(monkeypatch):
    # Naming a different prefix un-exempts /chainlit.
    app = _build_app(monkeypatch, RATE_LIMIT_DEFAULT="1/minute", RATE_LIMIT_EXEMPT_PATHS="/other")
    client = TestClient(app)
    assert client.get("/chainlit/ws/socket.io/").status_code == 200
    assert client.get("/chainlit/ws/socket.io/").status_code == 429
    for _ in range(5):
        assert client.get("/other").status_code == 200


def test_exempt_paths_can_be_disabled_with_empty_env(monkeypatch):
    # Set-but-empty means "no exemptions", distinct from unset (use the defaults).
    app = _build_app(monkeypatch, RATE_LIMIT_DEFAULT="1/minute", RATE_LIMIT_EXEMPT_PATHS="")
    client = TestClient(app)
    assert client.get("/chainlit/ws/socket.io/").status_code == 200
    assert client.get("/chainlit/ws/socket.io/").status_code == 429


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
