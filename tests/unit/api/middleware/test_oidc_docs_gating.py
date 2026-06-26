"""AuthMiddleware: interactive API docs are login-gated under AUTH_MODE=oidc.

The OpenAPI docs (``/docs``, ``/redoc``, ``/openapi.json``) bypass auth in token
mode (legacy contract) but expose the full route + schema surface, so serving
them anonymously in an OIDC deployment is undesirable. Under ``AUTH_MODE=oidc``
the middleware must instead treat them like any UI page: an unauthenticated
browser is 302-redirected to ``/auth/login``, while a valid session still
renders them. Health/version probes stay public in both modes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from api.middleware.auth import SESSION_COOKIE_NAME, AuthMiddleware
from core.config.auth import AuthBypassConfig
from fastapi import Request
from starlette.responses import Response


def _req(path: str, *, headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": raw,
        "query_string": b"",
        "client": ("1.2.3.4", 1234),
    }
    return Request(scope)


async def _call_next(_request) -> Response:
    return Response("ok")


def _anon_service() -> AsyncMock:
    svc = AsyncMock()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value=None)
    return svc


def _authed_service() -> AsyncMock:
    session = {"id": 7, "user_id": 1}
    svc = AsyncMock()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=session)
    svc.refresh_session_if_needed = AsyncMock(return_value=session)
    svc.get_user_for_request = AsyncMock(return_value={"id": 1, "is_admin": True})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    return svc


def _middleware(svc, *, bypass_config: AuthBypassConfig | None = None) -> AuthMiddleware:
    return AuthMiddleware(
        lambda scope, receive, send: None,
        get_auth_service=lambda _r: svc,
        bypass_config=bypass_config,
    )


@pytest.fixture(autouse=True)
def _oidc_env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_TOKEN_ENCRYPTION_KEY", "x")
    # Keep the auth-failure limiter out of the way of these dispatch assertions.
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_oidc_anonymous_docs_redirect_to_login(path) -> None:
    mw = _middleware(_anon_service())
    resp = await mw.dispatch(_req(path), _call_next)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/auth/login?next={path.replace('/', '%2F')}"


@pytest.mark.asyncio
async def test_oidc_authenticated_session_reaches_docs() -> None:
    captured: dict = {}

    async def call_next(req):
        captured["user"] = req.state.user
        return Response("ok")

    mw = _middleware(_authed_service())
    resp = await mw.dispatch(_req("/docs", headers={"cookie": f"{SESSION_COOKIE_NAME}=tok"}), call_next)
    assert resp.status_code == 200
    assert captured["user"] == {"id": 1, "is_admin": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health_check", "/version", "/auth/login"])
async def test_oidc_non_docs_bypass_paths_stay_public(path) -> None:
    """Only the docs are gated — health/version/auth callbacks still bypass."""
    mw = _middleware(_anon_service())
    resp = await mw.dispatch(_req(path), _call_next)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_token_mode_docs_stay_public(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    mw = _middleware(_anon_service())
    resp = await mw.dispatch(_req("/docs"), _call_next)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_empty_oidc_gated_paths_restores_public_docs() -> None:
    """Operators can opt back into anonymous docs under oidc by clearing the set."""
    mw = _middleware(_anon_service(), bypass_config=AuthBypassConfig(oidc_gated_paths=()))
    resp = await mw.dispatch(_req("/docs"), _call_next)
    assert resp.status_code == 200
