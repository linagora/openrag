"""Tests for the AuthBypassConfig plumbing in api.middleware.auth.

Defaults preserve the legacy module-level frozensets; passing a custom
:class:`AuthBypassConfig` to :class:`AuthMiddleware` (or the helper
functions) overrides the bypass policy at construction time. This is
the contract that satisfies the Phase 10C "config-driven bypass paths"
requirement.
"""

from __future__ import annotations

import pytest
from api.middleware.auth import (
    AuthMiddleware,
    is_bypass_path,
    is_ui_path,
)
from core.config.auth import (
    DEFAULT_API_PREFIXES,
    DEFAULT_BYPASS_PATHS,
    DEFAULT_UI_PATH_PREFIXES,
    AuthBypassConfig,
)
from fastapi import FastAPI, Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Defaults match the legacy hardcoded sets
# ---------------------------------------------------------------------------


def test_default_bypass_paths_match_legacy_set() -> None:
    """The Phase 10C move must be behaviour-preserving. ``/docs``,
    ``/health_check``, ``/auth/callback`` etc. were hardcoded in the
    legacy module-level frozenset; AuthBypassConfig() reproduces that
    exact list."""
    expected = {
        "/docs",
        "/openapi.json",
        "/redoc",
        "/health_check",
        "/version",
        "/auth/login",
        "/auth/callback",
        "/auth/backchannel-logout",
        "/auth/logout",
    }
    assert set(DEFAULT_BYPASS_PATHS) == expected
    assert set(AuthBypassConfig().bypass_paths) == expected


def test_default_api_prefixes_match_legacy_set() -> None:
    expected = {
        "/v1/",
        "/indexer/",
        "/search/",
        "/users/",
        "/partition/",
        "/workspaces/",
        "/queue/",
        "/extract/",
        "/actors/",
        "/monitoring/",
        "/tools/",
    }
    assert set(DEFAULT_API_PREFIXES) == expected
    assert set(AuthBypassConfig().api_prefixes) == expected


def test_default_ui_path_prefixes_match_legacy_set() -> None:
    assert set(DEFAULT_UI_PATH_PREFIXES) == {"/static"}
    assert set(AuthBypassConfig().ui_path_prefixes) == {"/static"}


# ---------------------------------------------------------------------------
# Module-level helpers fall back to defaults when no config passed
# ---------------------------------------------------------------------------


def test_is_bypass_path_default_matches_legacy_behaviour() -> None:
    """Legacy 30+ tests in components/auth/test_middleware.py call
    ``is_bypass_path(path)`` without a config kwarg — the default
    must still mirror the original frozenset for those to pass."""
    assert is_bypass_path("/docs") is True
    assert is_bypass_path("/health_check") is True
    assert is_bypass_path("/chainlit") is True
    assert is_bypass_path("/chainlit/sub") is True
    assert is_bypass_path("/v1/chat/completions") is False
    # #359 regression: only the actual /chainlit subtree bypasses.
    assert is_bypass_path("/chainlitevil") is False


def test_is_ui_path_default_matches_legacy_behaviour() -> None:
    assert is_ui_path("/") is True
    assert is_ui_path("/static/file.pdf") is True
    assert is_ui_path("/v1/chat/completions") is False
    assert is_ui_path("/indexer/foo") is False


# ---------------------------------------------------------------------------
# Custom config overrides the policy
# ---------------------------------------------------------------------------


def test_custom_bypass_paths_take_effect() -> None:
    """An :class:`AuthBypassConfig` with a tighter list narrows the
    bypass set — useful when an operator wants ``/docs`` gated behind
    auth."""
    cfg = AuthBypassConfig(bypass_paths=("/version",))
    assert is_bypass_path("/version", bypass_config=cfg) is True
    # /docs is bypassed by default but not under the tightened config.
    assert is_bypass_path("/docs", bypass_config=cfg) is False
    # Chainlit subtree is hardcoded in addition to the configured list —
    # this is intentional (chainlit handles its own header-auth).
    assert is_bypass_path("/chainlit", bypass_config=cfg) is True


def test_custom_api_prefixes_change_ui_classification() -> None:
    """Adding a new prefix to ``api_prefixes`` reclassifies that
    subtree as API (no /auth/login redirect) — what an operator
    would do when mounting a new programmatic router."""
    cfg = AuthBypassConfig(api_prefixes=DEFAULT_API_PREFIXES + ("/admin-api/",))
    assert is_ui_path("/admin-api/things", bypass_config=cfg) is False
    # Without the override the helper has no opinion and falls back to
    # the default UI prefixes (which don't include /admin-api/).
    assert is_ui_path("/admin-api/things") is False


def test_custom_ui_path_prefixes_widen_redirect_set() -> None:
    """Adding ``/portal`` to ``ui_path_prefixes`` makes unauthenticated
    /portal/* requests in oidc mode redirect to /auth/login instead of
    returning JSON 401."""
    cfg = AuthBypassConfig(ui_path_prefixes=("/static", "/portal"))
    assert is_ui_path("/portal/dashboard", bypass_config=cfg) is True
    assert is_ui_path("/portal/dashboard") is False  # default — still API


# ---------------------------------------------------------------------------
# AuthMiddleware carries the config and threads it through dispatch
# ---------------------------------------------------------------------------


def test_auth_middleware_uses_default_bypass_config_when_unspecified() -> None:
    app = FastAPI()
    app.add_middleware(AuthMiddleware, get_auth_service=lambda _request: None)
    # The wrapper Starlette builds is one BaseHTTPMiddleware layer over
    # AuthMiddleware; reach into it to confirm the default policy is
    # the one carried by the middleware instance.
    user_mw = next(m for m in app.user_middleware if m.cls is AuthMiddleware)
    instance = user_mw.cls(app, **user_mw.kwargs)
    assert isinstance(instance._bypass_config, AuthBypassConfig)
    assert set(instance._bypass_config.bypass_paths) == set(DEFAULT_BYPASS_PATHS)


def test_auth_middleware_accepts_custom_bypass_config() -> None:
    app = FastAPI()
    custom = AuthBypassConfig(bypass_paths=("/only-this",))
    app.add_middleware(
        AuthMiddleware,
        get_auth_service=lambda _request: None,
        bypass_config=custom,
    )
    user_mw = next(m for m in app.user_middleware if m.cls is AuthMiddleware)
    instance = user_mw.cls(app, **user_mw.kwargs)
    assert instance._bypass_config is custom


def _request(headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": "/indexer/files", "headers": raw, "query_string": b""}
    return Request(scope)


async def _unused_call_next(_request):
    return Response("ok")


@pytest.mark.asyncio
async def test_auth_middleware_returns_503_when_auth_service_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")

    def unavailable(_request):
        raise RuntimeError("container unavailable")

    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=unavailable)

    response = await middleware.dispatch(_request(headers={"authorization": "Bearer token"}), _unused_call_next)

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_auth_middleware_does_not_swallow_programming_errors(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")

    def broken(_request):
        raise ValueError("unexpected bug")

    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=broken)

    with pytest.raises(ValueError, match="unexpected bug"):
        await middleware.dispatch(_request(headers={"authorization": "Bearer token"}), _unused_call_next)


# ---------------------------------------------------------------------------
# Minor: argument is keyword-only so we don't accidentally pass it positionally
# ---------------------------------------------------------------------------


def test_is_bypass_path_bypass_config_is_keyword_only() -> None:
    """Positional misuse should fail loudly rather than silently
    treating an arbitrary value as the config."""
    with pytest.raises(TypeError):
        is_bypass_path("/docs", AuthBypassConfig())  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Dev bypass (AUTH_MODE=token, AUTH_TOKEN unset) requires ALLOW_NO_AUTH=true
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dev_bypass_resolves_admin_when_allow_no_auth_set(monkeypatch) -> None:
    """With ALLOW_NO_AUTH=true the no-token bypass resolves admin user 1."""
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_NO_AUTH", "true")

    svc = type("S", (), {})()
    svc.get_user_for_request = AsyncMock(return_value={"id": 1, "display_name": "Admin"})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])

    captured = {}

    async def call_next(req):
        captured["user"] = req.state.user
        return Response("ok")

    middleware = AuthMiddleware(
        lambda scope, receive, send: None, get_auth_service=lambda _r: svc
    )
    response = await middleware.dispatch(_request(), call_next)

    assert response.status_code == 200
    svc.get_user_for_request.assert_awaited_with(1)
    assert captured["user"] == {"id": 1, "display_name": "Admin"}


@pytest.mark.asyncio
async def test_dev_bypass_does_not_fail_open_without_flag(monkeypatch) -> None:
    """Without ALLOW_NO_AUTH a missing AUTH_TOKEN must NOT fail open to admin."""
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_NO_AUTH", raising=False)

    svc = type("S", (), {})()
    svc.get_user_for_request = AsyncMock()
    svc.list_user_partitions_for_request = AsyncMock()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)

    async def call_next(req):
        return Response("ok")

    middleware = AuthMiddleware(
        lambda scope, receive, send: None, get_auth_service=lambda _r: svc
    )
    response = await middleware.dispatch(_request(), call_next)

    # No token + no opt-in → not authenticated, never resolves to admin.
    assert response.status_code in (401, 403)
    svc.get_user_for_request.assert_not_awaited()


def test_request_object_is_unused_by_helpers() -> None:
    """Sanity: ``is_ui_path`` / ``is_bypass_path`` are pure functions
    over the string path, so a FastAPI ``Request`` is never required
    (or accepted) — guards against accidentally drifting them into
    needing a live request."""
    # Construct a path manually rather than from a Request — the
    # helpers must accept it.
    assert is_ui_path("/") is True
    # And the function signature has no Request parameter.
    import inspect

    sig = inspect.signature(is_ui_path)
    assert Request not in {p.annotation for p in sig.parameters.values()}
