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
    SESSION_COOKIE_NAME,
    AuthFailureRateLimiter,
    AuthMiddleware,
    is_bypass_path,
    is_ui_path,
)
from core.auth.chainlit import CHAINLIT_TOKEN_COOKIE_NAME
from core.config.auth import (
    DEFAULT_API_PREFIXES,
    DEFAULT_BYPASS_PATHS,
    DEFAULT_UI_PATH_PREFIXES,
    AuthBypassConfig,
)
from fastapi import FastAPI, Request
from starlette.responses import Response

_GENERATED_TOKEN = "or-" + "0123456789abcdef" * 2

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
        "/ready",
        "/version",
        "/auth/login",
        "/auth/callback",
        "/auth/backchannel-logout",
        "/auth/logout",
        "/auth/chainlit-logout-signal",
        "/metrics",
    }
    assert set(DEFAULT_BYPASS_PATHS) == expected
    assert set(AuthBypassConfig().bypass_paths) == expected


def test_metrics_is_bypassed_by_default() -> None:
    """Prometheus scrapes ``/metrics`` without a user token: the route enforces
    its own optional ``METRICS_TOKEN`` (see api.routers.admin.monitoring), so
    the middleware must not demand a bearer first."""
    assert is_bypass_path("/metrics")


def test_metrics_is_not_login_gated_under_oidc() -> None:
    """A scraper can't follow an IdP redirect; ``/metrics`` stays out of the
    oidc-gated subset."""
    assert "/metrics" not in AuthBypassConfig().oidc_gated_paths


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


def _request(headers=None, path="/indexer/files", app=None, client=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": path, "headers": raw, "query_string": b""}
    if app is not None:
        scope["app"] = app
    if client is not None:
        scope["client"] = client
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

    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
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

    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    response = await middleware.dispatch(_request(), call_next)

    # No token + no opt-in → not authenticated, never resolves to admin.
    assert response.status_code in (401, 403)
    svc.get_user_for_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_oidc_chainlit_html_allows_valid_token_handoff_cookie(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("AUTH_TOKEN", "secret")

    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 7, "display_name": "Token User"})

    async def call_next(req):
        return Response("chainlit")

    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    response = await middleware.dispatch(
        _request(
            path="/chainlit/",
            headers={
                "accept": "text/html",
                "cookie": f"{CHAINLIT_TOKEN_COOKIE_NAME}=or-user-token",
            },
        ),
        call_next,
    )

    assert response.status_code == 200
    svc.get_user_by_token_for_request.assert_awaited_once_with("or-user-token")


@pytest.mark.asyncio
async def test_failed_token_auth_is_rate_limited_by_ip(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")

    async def call_next(req):
        return Response("ok")

    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: None)

    first = await middleware.dispatch(_request(), call_next)
    second = await middleware.dispatch(_request(), call_next)

    assert first.status_code == 403
    assert second.status_code == 429
    assert second.headers.get("Retry-After")


@pytest.mark.asyncio
async def test_failed_bearer_limit_blocks_before_token_lookup(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")

    svc = type("S", (), {})()
    svc.get_user_by_token_for_request = AsyncMock(return_value=None)

    async def call_next(req):
        return Response("ok")

    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    first = await middleware.dispatch(_request(headers={"authorization": "Bearer bad"}), call_next)
    second = await middleware.dispatch(_request(headers={"authorization": "Bearer bad"}), call_next)

    assert first.status_code == 403
    assert second.status_code == 429
    svc.get_user_by_token_for_request.assert_awaited_once_with("bad")


@pytest.mark.asyncio
async def test_only_generated_recent_tokens_pass_exhausted_failure_bucket(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "configured-admin-token")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")

    user = {"id": 7, "display_name": "Token User"}

    async def get_user(token):
        return user if token in {_GENERATED_TOKEN, "configured-admin-token"} else None

    svc = type("S", (), {})()
    svc.get_user_by_token_for_request = AsyncMock(side_effect=get_user)
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    generated_token = await middleware.dispatch(
        _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}), _unused_call_next
    )
    hand_chosen_token = await middleware.dispatch(
        _request(headers={"authorization": "Bearer configured-admin-token"}), _unused_call_next
    )
    failed_token = await middleware.dispatch(_request(headers={"authorization": "Bearer bad-token"}), _unused_call_next)
    hand_chosen_token_after_lockout = await middleware.dispatch(
        _request(headers={"authorization": "Bearer configured-admin-token"}), _unused_call_next
    )
    generated_token_after_lockout = await middleware.dispatch(
        _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}), _unused_call_next
    )

    assert generated_token.status_code == 200
    assert hand_chosen_token.status_code == 200
    assert failed_token.status_code == 403
    assert hand_chosen_token_after_lockout.status_code == 429
    assert generated_token_after_lockout.status_code == 200
    assert [call.args[0] for call in svc.get_user_by_token_for_request.await_args_list] == [
        _GENERATED_TOKEN,
        "configured-admin-token",
        "bad-token",
        _GENERATED_TOKEN,
    ]


@pytest.mark.asyncio
async def test_recently_authenticated_token_does_not_bypass_another_ip_bucket(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "configured-admin-token")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")

    user = {"id": 7, "display_name": "Token User"}

    async def get_user(token):
        return user if token == _GENERATED_TOKEN else None

    svc = type("S", (), {})()
    svc.get_user_by_token_for_request = AsyncMock(side_effect=get_user)
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    known_token = await middleware.dispatch(
        _request(
            headers={"authorization": f"Bearer {_GENERATED_TOKEN}"},
            client=("192.0.2.10", 12345),
        ),
        _unused_call_next,
    )
    failed_token = await middleware.dispatch(
        _request(headers={"authorization": "Bearer bad-token"}, client=("192.0.2.20", 12345)),
        _unused_call_next,
    )
    cross_ip_token = await middleware.dispatch(
        _request(
            headers={"authorization": f"Bearer {_GENERATED_TOKEN}"},
            client=("192.0.2.20", 12345),
        ),
        _unused_call_next,
    )

    assert known_token.status_code == 200
    assert failed_token.status_code == 403
    assert cross_ip_token.status_code == 429
    assert [call.args[0] for call in svc.get_user_by_token_for_request.await_args_list] == [
        _GENERATED_TOKEN,
        "bad-token",
    ]


@pytest.mark.asyncio
async def test_concurrent_revoked_token_requests_share_one_lookup(monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "configured-admin-token")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")

    token_is_valid = True
    lookup_started = asyncio.Event()
    finish_lookup = asyncio.Event()
    user = {"id": 7, "display_name": "Token User"}

    async def get_user(token):
        if token == _GENERATED_TOKEN and token_is_valid:
            return user
        if token == _GENERATED_TOKEN:
            lookup_started.set()
            await finish_lookup.wait()
        return None

    svc = type("S", (), {})()
    svc.get_user_by_token_for_request = AsyncMock(side_effect=get_user)
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    client = ("192.0.2.30", 12345)

    known = await middleware.dispatch(
        _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}, client=client), _unused_call_next
    )
    failed = await middleware.dispatch(
        _request(headers={"authorization": "Bearer bad-token"}, client=client), _unused_call_next
    )
    assert known.status_code == 200
    assert failed.status_code == 403

    token_is_valid = False
    tasks = [
        asyncio.create_task(
            middleware.dispatch(
                _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}, client=client), _unused_call_next
            )
        )
    ]
    try:
        await asyncio.wait_for(lookup_started.wait(), timeout=5)
        second_precheck_seen = asyncio.Event()
        original_cache_check = middleware._has_recently_authenticated_token

        def count_second_precheck(identity, credential, kind):
            if credential == _GENERATED_TOKEN:
                second_precheck_seen.set()
            return original_cache_check(identity, credential, kind)

        monkeypatch.setattr(middleware, "_has_recently_authenticated_token", count_second_precheck)
        tasks.append(
            asyncio.create_task(
                middleware.dispatch(
                    _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}, client=client), _unused_call_next
                )
            )
        )
        await asyncio.wait_for(second_precheck_seen.wait(), timeout=5)
        finish_lookup.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert [response.status_code for response in responses] == [429, 429]
    assert [call.args[0] for call in svc.get_user_by_token_for_request.await_args_list] == [
        _GENERATED_TOKEN,
        "bad-token",
        _GENERATED_TOKEN,
    ]
    assert middleware._credential_locks == {}


@pytest.mark.asyncio
async def test_revoked_recent_token_is_removed_after_failed_revalidation(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "configured-admin-token")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")

    token_is_valid = True
    user = {"id": 7, "display_name": "Token User"}

    async def get_user(token):
        return user if token == _GENERATED_TOKEN and token_is_valid else None

    svc = type("S", (), {})()
    svc.get_user_by_token_for_request = AsyncMock(side_effect=get_user)
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    first = await middleware.dispatch(
        _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}), _unused_call_next
    )
    failed = await middleware.dispatch(_request(headers={"authorization": "Bearer bad-token"}), _unused_call_next)
    token_is_valid = False
    revoked = await middleware.dispatch(
        _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}), _unused_call_next
    )
    revoked_retry = await middleware.dispatch(
        _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}), _unused_call_next
    )

    assert first.status_code == 200
    assert failed.status_code == 403
    assert revoked.status_code == 429
    assert revoked_retry.status_code == 429
    assert [call.args[0] for call in svc.get_user_by_token_for_request.await_args_list] == [
        _GENERATED_TOKEN,
        "bad-token",
        _GENERATED_TOKEN,
    ]


def test_recent_token_cache_covers_late_failure_window(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: None)

    now = [100.0]
    monkeypatch.setattr("api.middleware.auth.time.monotonic", lambda: now[0])
    middleware._remember_authenticated_token("ip:unknown", _GENERATED_TOKEN, "token")

    # A failure bucket can fill at the end of the first 60-second window and
    # remain exhausted until that last failure expires near the 120-second mark.
    now[0] = 219.9
    assert middleware._has_recently_authenticated_token("ip:unknown", _GENERATED_TOKEN, "token")
    now[0] = 220.0
    assert not middleware._has_recently_authenticated_token("ip:unknown", _GENERATED_TOKEN, "token")


def test_recent_token_cache_evicts_oldest_entry_at_capacity(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: None)
    identity = "ip:unknown"

    for index in range(1025):
        middleware._remember_authenticated_token(identity, f"or-{index:032x}", "token")

    assert len(middleware._recently_authenticated_tokens) == 1024
    assert not middleware._has_recently_authenticated_token(identity, f"or-{0:032x}", "token")
    assert middleware._has_recently_authenticated_token(identity, f"or-{1024:032x}", "token")


def test_recent_token_cache_purges_expired_entries_in_validation_order(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: None)
    identity = "ip:unknown"

    now = [100.0]
    monkeypatch.setattr("api.middleware.auth.time.monotonic", lambda: now[0])
    middleware._remember_authenticated_token(identity, "or-00000000000000000000000000000002", "token")
    now[0] = 110.0
    middleware._remember_authenticated_token(identity, "or-00000000000000000000000000000003", "token")
    assert middleware._has_recently_authenticated_token(identity, "or-00000000000000000000000000000002", "token")

    now[0] = 220.0
    middleware._remember_authenticated_token(identity, "or-00000000000000000000000000000004", "token")

    assert len(middleware._recently_authenticated_tokens) == 2
    assert middleware._has_recently_authenticated_token(identity, "or-00000000000000000000000000000003", "token")


def test_recently_rejected_session_cache_is_bounded_and_renewable(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: None)
    identity = "ip:unknown"
    now = [100.0]
    monkeypatch.setattr("api.middleware.auth.time.monotonic", lambda: now[0])

    for index in range(1025):
        middleware._remember_rejected_session(identity, f"session-{index}")

    assert len(middleware._recently_rejected_sessions) == 1024
    assert not middleware._has_recently_rejected_session(identity, "session-0")
    assert middleware._has_recently_rejected_session(identity, "session-1024")

    now[0] = 219.9
    middleware._remember_rejected_session(identity, "session-1024")
    now[0] = 339.8
    assert middleware._has_recently_rejected_session(identity, "session-1024")
    now[0] = 340.0
    assert not middleware._has_recently_rejected_session(identity, "session-1024")


@pytest.mark.asyncio
async def test_recent_oidc_cookie_passes_exhausted_failure_bucket(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("AUTH_TOKEN", "configured-admin-token")
    monkeypatch.setenv("OIDC_TOKEN_ENCRYPTION_KEY", "test-key")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")

    session = {"id": 3, "user_id": 7}
    user = {"id": 7, "display_name": "OIDC User"}
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(
        side_effect=lambda token: session if token == "known-session-token" else None
    )
    svc.oidc_session_needs_refresh = lambda _session: False
    svc.get_user_for_request = AsyncMock(return_value=user)
    svc.get_user_by_token_for_request = AsyncMock(return_value=None)
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    known_session = await middleware.dispatch(
        _request(headers={"cookie": f"{SESSION_COOKIE_NAME}=known-session-token"}), _unused_call_next
    )
    failed_token = await middleware.dispatch(_request(headers={"authorization": "Bearer bad-token"}), _unused_call_next)
    known_session_after_lockout = await middleware.dispatch(
        _request(headers={"cookie": f"{SESSION_COOKIE_NAME}=known-session-token"}), _unused_call_next
    )

    assert known_session.status_code == 200
    assert failed_token.status_code == 401
    assert known_session_after_lockout.status_code == 200
    assert [call.args[0] for call in svc.get_oidc_session_by_token_for_request.await_args_list] == [
        "known-session-token",
        "bad-token",
        "known-session-token",
    ]


@pytest.mark.asyncio
async def test_oidc_cookie_miss_preserves_programmatic_token_eligibility(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 7})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    bearer = _request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"})
    cookie = _request(path="/", headers={"cookie": f"{SESSION_COOKIE_NAME}={_GENERATED_TOKEN}"})

    assert (await middleware.dispatch(bearer, _unused_call_next)).status_code == 200
    assert (await middleware.dispatch(cookie, _unused_call_next)).status_code == 302
    await middleware._auth_failure_limiter.record_failure(bearer)
    svc.get_oidc_session_by_token_for_request.reset_mock()

    assert (await middleware.dispatch(cookie, _unused_call_next)).status_code == 429
    assert (await middleware.dispatch(bearer, _unused_call_next)).status_code == 200
    svc.get_oidc_session_by_token_for_request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_succeeds", [True, False])
async def test_oidc_refresh_is_shared_by_cookie_and_limited_bearer_requests(monkeypatch, refresh_succeeds) -> None:
    import asyncio
    from datetime import datetime, timedelta
    from unittest.mock import AsyncMock

    from services.auth.refresh import session_was_refreshed_since

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    start_refresh = asyncio.Event()
    finish_refresh = asyncio.Event()
    bearer_rechecked_bucket = asyncio.Event()
    stored_session = {
        "id": 3,
        "user_id": 7,
        "fresh": True,
        "last_refresh_at": datetime.now() - timedelta(minutes=1),
        "access_token_expires_at": datetime.now() + timedelta(hours=1),
    }
    exchanges = 0

    async def refresh(*, session, enc_key):
        nonlocal exchanges
        if not session["fresh"]:
            exchanges += 1
            start_refresh.set()
            await finish_refresh.wait()
            if not refresh_succeeds:
                return None
            stored_session["fresh"] = True
            stored_session["last_refresh_at"] = datetime.now()
            # This remains inside the middleware's 60-second refresh buffer.
            stored_session["access_token_expires_at"] = datetime.now() + timedelta(seconds=60)
            return stored_session.copy()
        return session

    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(side_effect=lambda _token: stored_session.copy())
    svc.oidc_session_needs_refresh = lambda session: not session["fresh"]
    svc.oidc_session_was_refreshed_since = session_was_refreshed_since
    svc.refresh_session_if_needed = AsyncMock(side_effect=refresh)
    svc.get_user_for_request = AsyncMock(return_value={"id": 7})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    svc.revoke_oidc_session_by_id_for_request = AsyncMock()
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    limiter = middleware._auth_failure_limiter
    cookie_ip = "192.0.2.20" if refresh_succeeds else "192.0.2.30"
    cookie = _request(headers={"cookie": f"{SESSION_COOKIE_NAME}=session-token"}, client=(cookie_ip, 12345))
    bearer = _request(headers={"authorization": "Bearer session-token"}, client=("192.0.2.30", 12345))
    assert (await middleware.dispatch(bearer, _unused_call_next)).status_code == 200
    stored_session["fresh"] = False
    original_check = limiter.is_limited
    bearer_checks = 0

    async def observe_bucket_check(request):
        nonlocal bearer_checks
        limited = await original_check(request)
        if request is bearer:
            bearer_checks += 1
            if bearer_checks == 2:
                bearer_rechecked_bucket.set()
        return limited

    monkeypatch.setattr(limiter, "is_limited", observe_bucket_check)
    tasks = [asyncio.create_task(middleware.dispatch(cookie, _unused_call_next))]
    try:
        await asyncio.wait_for(start_refresh.wait(), timeout=5)
        await limiter.record_failure(bearer)
        tasks.append(asyncio.create_task(middleware.dispatch(bearer, _unused_call_next)))
        await asyncio.wait_for(bearer_rechecked_bucket.wait(), timeout=5)
        finish_refresh.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert exchanges == 1
    svc.refresh_session_if_needed.assert_awaited_once()
    if refresh_succeeds:
        assert [response.status_code for response in responses] == [200, 200]
        svc.revoke_oidc_session_by_id_for_request.assert_not_awaited()
        assert svc.get_oidc_session_by_token_for_request.await_count == 5
    else:
        assert [response.status_code for response in responses] == [429, 429]
        svc.revoke_oidc_session_by_id_for_request.assert_awaited_once_with(3)
        # The limited waiter skips the second read after the cache eviction.
        assert svc.get_oidc_session_by_token_for_request.await_count == 4
    assert middleware._credential_locks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["session", "user"])
async def test_fresh_oidc_session_reads_are_concurrent(monkeypatch, read) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    session = {"id": 3, "user_id": 7}
    user = {"id": 7}
    both_reading = asyncio.Event()
    reads = 0

    async def concurrent_read(_key):
        nonlocal reads
        reads += 1
        if reads == 2:
            both_reading.set()
        await asyncio.wait_for(both_reading.wait(), timeout=5)
        return session if read == "session" else user

    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = (
        concurrent_read if read == "session" else AsyncMock(return_value=session)
    )
    svc.oidc_session_needs_refresh = lambda _session: False
    svc.refresh_session_if_needed = AsyncMock()
    svc.get_user_for_request = concurrent_read if read == "user" else AsyncMock(return_value=user)
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    responses = await asyncio.wait_for(
        asyncio.gather(
            *(
                middleware.dispatch(
                    _request(headers={"cookie": f"{SESSION_COOKIE_NAME}=session-token"}), _unused_call_next
                )
                for _ in range(2)
            )
        ),
        timeout=10,
    )

    assert [response.status_code for response in responses] == [200, 200]
    svc.refresh_session_if_needed.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_renews_eligibility_before_releasing_the_session_lock(monkeypatch) -> None:
    """A same-IP limited request whose cache entry expires while it waits on
    the session lock still authenticates once the concurrent refresh
    succeeds, even though the failure bucket stays full.

    The refresh owner here is an unrestricted request (it passed its bucket
    check before the bucket filled), so only the session lock coordinates
    the two — the outer eligibility lock in dispatch does not.
    """
    import asyncio
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.auth.refresh import session_was_refreshed_since

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    cache_time = 100.0
    monkeypatch.setattr("api.middleware.auth.time", SimpleNamespace(monotonic=lambda: cache_time))
    start_refresh = asyncio.Event()
    finish_refresh = asyncio.Event()
    waiter_reached_session_read = asyncio.Event()
    stored_session = {
        "id": 3,
        "user_id": 7,
        "fresh": False,
        "last_refresh_at": datetime.now() - timedelta(minutes=1),
        "access_token_expires_at": datetime.now() + timedelta(hours=1),
    }

    async def refresh(*, session, enc_key):
        start_refresh.set()
        await finish_refresh.wait()
        stored_session["fresh"] = True
        stored_session["last_refresh_at"] = datetime.now()
        # Still inside the middleware's 60s refresh buffer, so the re-read
        # under the lock cannot be skipped via oidc_session_was_refreshed_since.
        stored_session["access_token_expires_at"] = datetime.now() + timedelta(seconds=60)
        return stored_session.copy()

    session_reads = 0

    async def lookup_session(_token):
        nonlocal session_reads
        session_reads += 1
        # Read #3 is the waiter's first (pre-lock) session read: the owner
        # did reads #1 (pre-lock) and #2 (under the lock). Signal it so the
        # test can let the cache entry expire before releasing the refresh.
        if session_reads == 3:
            waiter_reached_session_read.set()
        return stored_session.copy()

    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(side_effect=lookup_session)
    svc.oidc_session_needs_refresh = lambda session: not session["fresh"]
    svc.oidc_session_was_refreshed_since = session_was_refreshed_since
    svc.refresh_session_if_needed = AsyncMock(side_effect=refresh)

    async def lookup_user(user_id):
        # Yield so the waiter can run between the refresh owner releasing
        # the session lock and renewing the cache entry — the window the
        # review found.
        await asyncio.sleep(0)
        return {"id": user_id}

    svc.get_user_for_request = AsyncMock(side_effect=lookup_user)
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    svc.revoke_oidc_session_by_id_for_request = AsyncMock()
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    limiter = middleware._auth_failure_limiter

    # Same client identity for both requests (the shared-proxy case): they
    # share the recent-success cache entry and the failure bucket.
    owner = _request(headers={"cookie": f"{SESSION_COOKIE_NAME}=session-token"}, client=("192.0.2.40", 12345))
    waiter = _request(headers={"cookie": f"{SESSION_COOKIE_NAME}=session-token"}, client=("192.0.2.40", 12346))
    identity = limiter._identity(owner)
    assert limiter._identity(waiter) == identity
    # One shared cache entry, valid at the waiter's eligibility checks but
    # expiring while the waiter waits for the session lock.
    middleware._remember_authenticated_token(identity, "session-token", "session")

    async def is_limited(request):
        # The owner passed its bucket check before the bucket filled; every
        # check the waiter makes sees a full bucket.
        return request is waiter

    monkeypatch.setattr(limiter, "is_limited", is_limited)

    tasks = [asyncio.create_task(middleware.dispatch(owner, _unused_call_next))]
    try:
        await asyncio.wait_for(start_refresh.wait(), timeout=5)
        tasks.append(asyncio.create_task(middleware.dispatch(waiter, _unused_call_next)))
        await asyncio.wait_for(waiter_reached_session_read.wait(), timeout=5)
        # Let the shared cache entry expire while the waiter waits on the
        # session lock the owner still holds inside the refresh.
        cache_time += middleware._recently_authenticated_token_ttl
        finish_refresh.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert [response.status_code for response in responses] == [200, 200]
    svc.refresh_session_if_needed.assert_awaited_once()
    assert middleware._credential_locks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_reopened", [False, True])
async def test_oidc_bearer_fallback_rechecks_eligibility_after_cookie_lookup(monkeypatch, bucket_reopened) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 7})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    request = _request(
        path="/", headers={"cookie": f"{SESSION_COOKIE_NAME}=cookie", "authorization": f"Bearer {_GENERATED_TOKEN}"}
    )
    limiter = middleware._auth_failure_limiter
    identity = limiter._identity(request)
    middleware._remember_authenticated_token(identity, "cookie", "session")
    middleware._remember_authenticated_token(identity, _GENERATED_TOKEN, "token")
    await limiter.record_failure(request)

    async def lookup_cookie(_token):
        middleware._forget_authenticated_token(identity, _GENERATED_TOKEN, "token")
        return None

    svc.get_oidc_session_by_token_for_request = AsyncMock(side_effect=lookup_cookie)
    checks = [True, True, not bucket_reopened, not bucket_reopened]
    monkeypatch.setattr(limiter, "is_limited", AsyncMock(side_effect=checks))

    response = await middleware.dispatch(request, _unused_call_next)

    if bucket_reopened:
        assert response.status_code == 200
        svc.get_user_by_token_for_request.assert_awaited_once_with(_GENERATED_TOKEN)
        assert [call.args[0] for call in svc.get_oidc_session_by_token_for_request.await_args_list] == [
            "cookie",
            _GENERATED_TOKEN,
        ]
    else:
        assert response.status_code == 429
        svc.get_oidc_session_by_token_for_request.assert_awaited_once_with("cookie")
        svc.get_user_by_token_for_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchecked_cookie_after_refresh_wait_blocks_distinct_bearer(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    session = {"id": 3, "user_id": 1}
    svc = type("S", (), {})()
    svc.oidc_session_needs_refresh = lambda _session: True
    svc.get_user_for_request = AsyncMock(return_value={"id": 1})
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 2})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    request = _request(
        path="/indexer/files",
        headers={"cookie": f"{SESSION_COOKIE_NAME}=cookie", "authorization": f"Bearer {_GENERATED_TOKEN}"},
    )
    limiter = middleware._auth_failure_limiter
    identity = limiter._identity(request)
    middleware._remember_authenticated_token(identity, "cookie", "session")
    middleware._remember_authenticated_token(identity, _GENERATED_TOKEN, "token")
    await limiter.record_failure(request)
    await limiter.record_failure(request)

    async def lookup_cookie(_token):
        middleware._forget_authenticated_token(identity, "cookie", "session")
        return session

    svc.get_oidc_session_by_token_for_request = AsyncMock(side_effect=lookup_cookie)

    response = await middleware.dispatch(request, _unused_call_next)

    assert response.status_code == 429
    svc.get_user_by_token_for_request.assert_awaited_once_with(_GENERATED_TOKEN)


@pytest.mark.asyncio
async def test_uncached_bearer_is_checked_if_bucket_reopens_after_cached_cookie_miss(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)

    async def lookup_bearer(_token):
        assert middleware._credential_locks == {}
        return {"id": 7}

    svc.get_user_by_token_for_request = AsyncMock(side_effect=lookup_bearer)
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    request = _request(
        path="/indexer/files",
        headers={"cookie": f"{SESSION_COOKIE_NAME}=revoked-cookie", "authorization": "Bearer valid-api-token"},
    )
    limiter = middleware._auth_failure_limiter
    identity = limiter._identity(request)
    middleware._remember_authenticated_token(identity, "revoked-cookie", "session")
    await limiter.record_failure(request)
    monkeypatch.setattr(limiter, "is_limited", AsyncMock(side_effect=[True, True, False]))

    response = await middleware.dispatch(request, _unused_call_next)

    assert response.status_code == 200
    svc.get_user_by_token_for_request.assert_awaited_once_with("valid-api-token")
    assert [call.args[0] for call in svc.get_oidc_session_by_token_for_request.await_args_list] == [
        "revoked-cookie",
        "valid-api-token",
    ]


@pytest.mark.asyncio
async def test_cookie_precedence_is_restored_if_bucket_reopens_before_bearer_fallback(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    session = {"id": 3, "user_id": 1}
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(
        side_effect=lambda token: session if token == "valid-cookie" else None
    )
    svc.oidc_session_needs_refresh = lambda _session: False
    svc.get_user_for_request = AsyncMock(return_value={"id": 1})
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 2})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    request = _request(
        path="/indexer/files",
        headers={"cookie": f"{SESSION_COOKIE_NAME}=valid-cookie", "authorization": f"Bearer {_GENERATED_TOKEN}"},
    )
    limiter = middleware._auth_failure_limiter
    middleware._remember_authenticated_token(limiter._identity(request), _GENERATED_TOKEN, "token")
    monkeypatch.setattr(limiter, "is_limited", AsyncMock(side_effect=[True, True, False]))

    async def return_user_id(req):
        return Response(str(req.state.user["id"]))

    response = await middleware.dispatch(request, return_user_id)

    assert response.status_code == 200
    assert response.body == b"1"
    svc.get_user_by_token_for_request.assert_not_awaited()
    svc.get_oidc_session_by_token_for_request.assert_awaited_once_with("valid-cookie")


@pytest.mark.asyncio
async def test_successful_cached_cookie_needs_no_lower_priority_retry(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value={"id": 3, "user_id": 1})
    svc.oidc_session_needs_refresh = lambda _session: False
    svc.get_user_for_request = AsyncMock(return_value={"id": 1})
    svc.get_user_by_token_for_request = AsyncMock()
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    request = _request(
        path="/indexer/files",
        headers={"cookie": f"{SESSION_COOKIE_NAME}=valid-cookie", "authorization": f"Bearer {_GENERATED_TOKEN}"},
    )
    limiter = middleware._auth_failure_limiter
    middleware._remember_authenticated_token(limiter._identity(request), "valid-cookie", "session")
    check = AsyncMock(side_effect=[True, True, False])
    monkeypatch.setattr(limiter, "is_limited", check)

    response = await middleware.dispatch(request, _unused_call_next)

    assert response.status_code == 200
    assert check.await_count == 2
    svc.get_oidc_session_by_token_for_request.assert_awaited_once_with("valid-cookie")
    svc.get_user_for_request.assert_awaited_once_with(1)
    svc.get_user_by_token_for_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncached_cookie_blocks_cached_bearer_during_lockout(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value={"id": 3, "user_id": 1})
    svc.oidc_session_needs_refresh = lambda _session: False
    svc.get_user_for_request = AsyncMock(return_value={"id": 1})
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 2})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    request = _request(
        path="/indexer/files",
        headers={"cookie": f"{SESSION_COOKIE_NAME}=valid-cookie", "authorization": f"Bearer {_GENERATED_TOKEN}"},
    )
    limiter = middleware._auth_failure_limiter
    identity = limiter._identity(request)
    middleware._remember_authenticated_token(identity, _GENERATED_TOKEN, "token")
    await limiter.record_failure(request)

    response = await middleware.dispatch(request, _unused_call_next)

    assert response.status_code == 429
    svc.get_oidc_session_by_token_for_request.assert_not_awaited()
    svc.get_user_for_request.assert_not_awaited()
    svc.get_user_by_token_for_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_oidc_cookie_does_not_block_cached_bearer_during_lockout(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    now = [100.0]
    monkeypatch.setattr("api.middleware.auth.time.monotonic", lambda: now[0])
    session = {"id": 3, "user_id": 1}
    session_state = {"revoked-cookie": session}
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(side_effect=lambda token: session_state.get(token))
    svc.oidc_session_needs_refresh = lambda _session: False
    svc.get_user_for_request = AsyncMock(return_value={"id": 1})
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 2})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    cookie_request = _request(
        path="/indexer/files",
        headers={"cookie": f"{SESSION_COOKIE_NAME}=revoked-cookie", "authorization": f"Bearer {_GENERATED_TOKEN}"},
    )
    bearer_request = _request(path="/indexer/files", headers={"authorization": f"Bearer {_GENERATED_TOKEN}"})

    # Both credentials were valid before the OIDC session was revoked.
    assert (await middleware.dispatch(bearer_request, _unused_call_next)).status_code == 200
    assert (await middleware.dispatch(cookie_request, _unused_call_next)).status_code == 200

    session_state.clear()
    limiter = middleware._auth_failure_limiter
    await limiter.record_failure(cookie_request)
    await limiter.record_failure(cookie_request)

    first_after_revocation = await middleware.dispatch(cookie_request, _unused_call_next)
    now[0] = 219.9
    next_after_revocation = await middleware.dispatch(cookie_request, _unused_call_next)
    now[0] = 339.8
    later_after_revocation = await middleware.dispatch(cookie_request, _unused_call_next)

    assert first_after_revocation.status_code == 200
    assert next_after_revocation.status_code == 200
    assert later_after_revocation.status_code == 200
    assert middleware._has_recently_rejected_session(limiter._identity(cookie_request), "revoked-cookie")
    assert svc.get_user_by_token_for_request.await_count == 4


@pytest.mark.asyncio
async def test_bearer_session_probe_does_not_make_matching_cookie_exempt_during_lockout(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    configured_token = "hand-chosen-admin-token"
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 2})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    bearer_request = _request(headers={"authorization": f"Bearer {configured_token}"})
    attacker_request = _request(
        headers={
            "cookie": f"{SESSION_COOKIE_NAME}={configured_token}",
            "authorization": f"Bearer {_GENERATED_TOKEN}",
        }
    )
    limiter = middleware._auth_failure_limiter
    identity = limiter._identity(attacker_request)

    assert (await middleware.dispatch(bearer_request, _unused_call_next)).status_code == 200
    assert not middleware._has_recently_rejected_session(identity, configured_token)
    assert (
        await middleware.dispatch(_request(headers={"authorization": f"Bearer {_GENERATED_TOKEN}"}), _unused_call_next)
    ).status_code == 200
    await limiter.record_failure(attacker_request)
    await limiter.record_failure(attacker_request)

    response = await middleware.dispatch(attacker_request, _unused_call_next)

    assert response.status_code == 429


@pytest.mark.asyncio
async def test_refused_cookie_keeps_precedence_when_cookie_and_bearer_match(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 2})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    request = _request(
        headers={
            "cookie": f"{SESSION_COOKIE_NAME}={_GENERATED_TOKEN}",
            "authorization": f"Bearer {_GENERATED_TOKEN}",
        }
    )
    limiter = middleware._auth_failure_limiter

    assert (await middleware.dispatch(request, _unused_call_next)).status_code == 200
    assert middleware._has_recently_rejected_session(limiter._identity(request), _GENERATED_TOKEN)
    await limiter.record_failure(request)
    await limiter.record_failure(request)

    response = await middleware.dispatch(request, _unused_call_next)

    assert response.status_code == 200
    assert svc.get_user_by_token_for_request.await_count == 2


@pytest.mark.asyncio
async def test_expired_oidc_ui_credentials_redirect_without_exhausting_the_bucket(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "2/minute")

    session = {"id": 3, "user_id": 7}
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(
        side_effect=lambda token: session if token == "valid-session" else None
    )
    svc.oidc_session_needs_refresh = lambda _session: False
    svc.get_user_for_request = AsyncMock(return_value={"id": 7})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    for token in ("expired-session-1", "expired-session-2"):
        response = await middleware.dispatch(
            _request(path="/", headers={"cookie": f"{SESSION_COOKIE_NAME}={token}"}), _unused_call_next
        )
        assert response.status_code == 302

    valid = await middleware.dispatch(
        _request(path="/", headers={"cookie": f"{SESSION_COOKIE_NAME}=valid-session"}), _unused_call_next
    )
    assert valid.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_still_limited", [True, False])
async def test_failed_cached_oidc_ui_credential_rechecks_failure_bucket(monkeypatch, bucket_still_limited) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)
    request = _request(path="/", headers={"cookie": f"{SESSION_COOKIE_NAME}=revoked-cookie"})
    limiter = middleware._auth_failure_limiter
    middleware._remember_authenticated_token(limiter._identity(request), "revoked-cookie", "session")
    monkeypatch.setattr(
        limiter,
        "is_limited",
        AsyncMock(side_effect=[True, True, bucket_still_limited]),
    )

    response = await middleware.dispatch(request, _unused_call_next)

    assert response.status_code == (429 if bucket_still_limited else 302)
    if bucket_still_limited:
        assert response.headers.get("Retry-After")
    svc.get_oidc_session_by_token_for_request.assert_awaited_once_with("revoked-cookie")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recent", "status", "lookups"),
    [
        ((("cookie", "session"),), 429, ["cookie"]),
        (((_GENERATED_TOKEN, "token"),), 429, []),
        ((("cookie", "session"), (_GENERATED_TOKEN, "token")), 200, ["cookie"]),
    ],
)
async def test_oidc_lockout_only_validates_cached_credentials(monkeypatch, recent, status, lookups) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 7})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    request = _request(
        headers={"cookie": f"{SESSION_COOKIE_NAME}=cookie", "authorization": f"Bearer {_GENERATED_TOKEN}"}
    )
    limiter = middleware._auth_failure_limiter
    await limiter.record_failure(request)
    for token, kind in recent:
        middleware._remember_authenticated_token(limiter._identity(request), token, kind)

    response = await middleware.dispatch(request, _unused_call_next)

    assert response.status_code == status
    assert [call.args[0] for call in svc.get_oidc_session_by_token_for_request.await_args_list] == lookups
    if status == 429:
        svc.get_user_by_token_for_request.assert_not_awaited()
    else:
        svc.get_user_by_token_for_request.assert_awaited_once_with(_GENERATED_TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_reopened", [False, True])
async def test_lockout_rechecks_bucket_and_only_uses_locked_credentials(monkeypatch, bucket_reopened) -> None:
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "1/minute")
    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value={"id": 7})
    svc.list_user_partitions_for_request = AsyncMock(return_value=[])
    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: svc)

    request = _request(
        headers={"cookie": f"{SESSION_COOKIE_NAME}=cookie", "authorization": f"Bearer {_GENERATED_TOKEN}"}
    )
    limiter = middleware._auth_failure_limiter
    await limiter.record_failure(request)
    middleware._remember_authenticated_token(limiter._identity(request), "cookie", "session")
    original_lock = middleware._lock_recently_authenticated_tokens

    @asynccontextmanager
    async def lock_after_cache_changed(identity, credentials):
        assert credentials == ("cookie",)
        middleware._forget_authenticated_token(identity, "cookie", "session")
        middleware._remember_authenticated_token(identity, _GENERATED_TOKEN, "token")
        async with original_lock(identity, credentials):
            yield

    monkeypatch.setattr(middleware, "_lock_recently_authenticated_tokens", lock_after_cache_changed)
    monkeypatch.setattr(limiter, "is_limited", AsyncMock(side_effect=[True, not bucket_reopened]))

    response = await middleware.dispatch(request, _unused_call_next)

    assert response.status_code == (200 if bucket_reopened else 429)
    assert [call.args[0] for call in svc.get_oidc_session_by_token_for_request.await_args_list] == (
        ["cookie", _GENERATED_TOKEN] if bucket_reopened else []
    )
    assert middleware._credential_locks == {}


@pytest.mark.asyncio
async def test_disabled_auth_failure_limiter_ignores_malformed_limit(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_AUTH_FAILURE", "not-a-limit")

    async def call_next(req):
        return Response("ok")

    middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=lambda _r: None)
    response = await middleware.dispatch(_request(), call_next)

    assert response.status_code == 403


def test_auth_failure_log_value_is_single_line_and_bounded() -> None:
    value = AuthFailureRateLimiter._safe_log_value("ip:1.2.3.4\nextra\rdata\x00" + ("x" * 300), max_len=40)

    assert "\n" not in value
    assert "\r" not in value
    assert "\x00" not in value
    assert len(value) == 40


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


# ---------------------------------------------------------------------------
# /metrics reaches the route without a user token, in both auth modes
# ---------------------------------------------------------------------------


def _anonymous_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "client": ("10.0.0.9", 4321),
        }
    )


async def _route_reached(_request) -> Response:
    return Response("scraped", status_code=200)


def _anonymous_auth_service():
    from unittest.mock import AsyncMock

    svc = AsyncMock()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value=None)
    return svc


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["token", "oidc"])
async def test_anonymous_scrape_reaches_metrics_route(monkeypatch, auth_mode) -> None:
    """A Prometheus scraper sends no user credential. The middleware must hand
    ``/metrics`` straight to the route (which applies METRICS_TOKEN itself)
    instead of answering 403 "Missing token" (token mode) or redirecting to
    the IdP (oidc mode)."""
    monkeypatch.setenv("AUTH_MODE", auth_mode)
    monkeypatch.setenv("AUTH_TOKEN", "an-admin-token")
    monkeypatch.setenv("OIDC_TOKEN_ENCRYPTION_KEY", "x")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    mw = AuthMiddleware(
        lambda scope, receive, send: None,
        get_auth_service=lambda _r: _anonymous_auth_service(),
    )

    resp = await mw.dispatch(_anonymous_request("/metrics"), _route_reached)

    assert resp.status_code == 200
    assert resp.body == b"scraped"


# ---------------------------------------------------------------------------
# Path decisions use the routed path
# ---------------------------------------------------------------------------

# A Host header under which ``request.url.path`` reads differently from the
# path the router dispatches.
_MISMATCHED_HOST = "testserver/health_check?x="


def _auth_app(monkeypatch, *, auth_mode: str) -> FastAPI:
    from unittest.mock import AsyncMock

    monkeypatch.setenv("AUTH_MODE", auth_mode)
    monkeypatch.setenv("AUTH_TOKEN", "secret")

    svc = type("S", (), {})()
    svc.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)
    svc.get_user_by_token_for_request = AsyncMock(return_value=None)

    app = FastAPI()

    @app.get("/indexer/files")
    async def protected() -> dict[str, str]:
        return {"served": "yes"}

    @app.get("/static/{file_id}")
    async def static_file(file_id: str) -> dict[str, str]:
        return {"served": file_id}

    app.add_middleware(AuthMiddleware, get_auth_service=lambda _request: svc)
    return app


def test_bypass_check_uses_the_routed_path(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(_auth_app(monkeypatch, auth_mode="token"))

    response = client.get("/indexer/files", headers={"host": _MISMATCHED_HOST})

    assert response.status_code == 403
    assert response.json() == {"detail": "Missing token"}


def test_oidc_login_redirect_uses_the_routed_path_and_query(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(_auth_app(monkeypatch, auth_mode="oidc"))

    response = client.get(
        "/static/abc?page=2",
        headers={"host": _MISMATCHED_HOST, "accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login?next=%2Fstatic%2Fabc%3Fpage%3D2"


# ---------------------------------------------------------------------------
# A degraded boot must answer 503, not 500 (#937)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_middleware_returns_503_when_the_container_is_none(monkeypatch) -> None:
    """The real resolver on a degraded boot.

    `main.py` resolves the auth service through `di.providers.get_container`,
    which raises `HTTPException(503)` when the boot guard has set
    `app.state.container = None`. Middleware runs outside the router, so
    FastAPI's handlers never convert that — uncaught it escapes as a 500
    `[UNEXPECTED_ERROR]` on every authenticated request, which is the opposite
    of the "serving degraded (503)" the boot guard logged.

    Exercises the real `get_container`, not a stand-in that raises RuntimeError:
    the bug was precisely that the production resolver raises something else.
    """
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")

    from types import SimpleNamespace

    from di.providers import get_container

    # `get_container` reads `request.app.state.container`, so the request needs a
    # real app in its scope — a degraded one, exactly as the boot guard leaves it.
    degraded_app = SimpleNamespace(state=SimpleNamespace(container=None))
    request = _request(headers={"authorization": "Bearer token"}, app=degraded_app)

    middleware = AuthMiddleware(
        lambda scope, receive, send: None,
        get_auth_service=lambda req: get_container(req).auth_service,
    )

    response = await middleware.dispatch(request, _unused_call_next)

    # 503, and specifically not the 500 `[UNEXPECTED_ERROR]` the bug produced.
    assert response.status_code == 503
    # The resolver's own detail is relayed rather than flattened to a generic
    # string, so the log and the response agree on why the request failed.
    assert b"container is not available" in response.body


def test_main_resolves_the_auth_service_through_get_container() -> None:
    """The wiring is the fix. Reading `app.state.container.auth_service`
    directly raises AttributeError on a degraded boot, which no guard catches."""
    import pathlib

    # Read as text rather than importing: importing `api.main` pulls in the
    # chainlit entrypoint, which fails outside a running app.
    source = (pathlib.Path(__file__).resolve().parents[4] / "openrag" / "api" / "main.py").read_text(encoding="utf-8")

    assert "get_auth_service=lambda request: get_container(request).auth_service" in source
    assert "request.app.state.container.auth_service" not in source, (
        "the auth service is resolved off a possibly-None container again; "
        "AttributeError there is not caught and surfaces as a 500"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/models", "/health_check", "/ready"])
async def test_dev_bypass_returns_503_when_the_container_is_none(monkeypatch, path) -> None:
    """The ``ALLOW_NO_AUTH`` branch resolves the auth service before the bypass
    list, so on a degraded boot an unguarded call there turned *every* path —
    health and readiness included — into a 500, not only authenticated ones.
    """
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_NO_AUTH", "true")

    from types import SimpleNamespace

    from di.providers import get_container

    degraded_app = SimpleNamespace(state=SimpleNamespace(container=None))
    middleware = AuthMiddleware(
        lambda scope, receive, send: None,
        get_auth_service=lambda req: get_container(req).auth_service,
    )

    response = await middleware.dispatch(_request(path=path, app=degraded_app), _unused_call_next)

    assert response.status_code == 503
    assert b"container is not available" in response.body


@pytest.mark.asyncio
async def test_unavailable_resolver_status_headers_and_log_are_relayed(monkeypatch) -> None:
    """The resolver's response is relayed, not rebuilt: its headers survive (a
    ``Retry-After`` on a 503 is the natural case), and the log names the status
    the client actually received rather than a fixed one.

    Uses a non-503 status on purpose — with 503 a hardcoded status in the log or
    the response would pass unnoticed.
    """
    from fastapi import HTTPException
    from loguru import logger

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")

    def unavailable(_request):
        raise HTTPException(status_code=502, detail="upstream gone", headers={"Retry-After": "7"})

    captured: list[dict] = []
    handler_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"], msg=m.record["message"])), level="WARNING"
    )
    try:
        middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=unavailable)
        response = await middleware.dispatch(_request(headers={"authorization": "Bearer token"}), _unused_call_next)
    finally:
        logger.remove(handler_id)

    assert response.status_code == 502
    assert response.headers["retry-after"] == "7"
    assert b"upstream gone" in response.body
    logged = [r for r in captured if r["msg"] == "Auth service unavailable"]
    assert [r["status"] for r in logged] == [502]


@pytest.mark.asyncio
async def test_unavailable_resolver_log_escapes_the_request_path(monkeypatch) -> None:
    """The routed path is percent-decoded, so ``%0A`` reaches here as a real
    newline. Logged raw, it would forge a second line in the text log format."""
    from loguru import logger

    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.setenv("AUTH_TOKEN", "secret")

    def unavailable(_request):
        raise RuntimeError("container unavailable")

    captured: list[dict] = []
    handler_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"], msg=m.record["message"])), level="WARNING"
    )
    try:
        middleware = AuthMiddleware(lambda scope, receive, send: None, get_auth_service=unavailable)
        await middleware.dispatch(
            _request(headers={"authorization": "Bearer token"}, path="/v1/x\nFORGED line"), _unused_call_next
        )
    finally:
        logger.remove(handler_id)

    [logged] = [r for r in captured if r["msg"] == "Auth service unavailable"]
    assert "\n" not in logged["path"]
    assert logged["path"] == "/v1/x\\nFORGED line"
