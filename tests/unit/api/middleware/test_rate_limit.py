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

    app = Starlette(routes=[Route("/v1/chat", ok), Route("/auth/login", ok), Route("/other", ok)])
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


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
