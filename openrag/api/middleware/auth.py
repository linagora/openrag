"""AuthMiddleware (Phase 10C — moved from ``components/auth/middleware.py``).

Pure relocation. Phase 8 already swapped the original Ray-actor calls
(``vectordb.get_user.remote()``, ``vectordb.get_oidc_session_by_token.remote()``,
``vectordb.list_user_partitions.remote()``) for ``AuthService`` methods on
the request-scoped service container, so the dispatch logic moves
without modification. The middleware supports both auth modes:

* ``AUTH_MODE=token`` (legacy) — single Bearer token from
  ``Authorization: Bearer ...`` or ``?token=`` for ``/static``. Missing
  or invalid token returns **403** with the legacy JSON body — the
  Robot Framework suite under ``tests/api/`` asserts the exact shape.
* ``AUTH_MODE=oidc`` — cookie-based session set by the OIDC callback,
  with lazy access-token refresh. A Bearer token is still accepted as
  a fallback for programmatic clients. Missing/invalid credentials:

    - UI paths → **302** to ``/auth/login?next=<encoded>``
    - API paths → **401** JSON ``{"detail": "Unauthenticated"}``

Environment configuration (``AUTH_MODE``, ``AUTH_TOKEN``,
``OIDC_TOKEN_ENCRYPTION_KEY``) is read at **dispatch** time via
``os.getenv`` so tests can monkeypatch ``os.environ`` per case.

Phase 10G migrated the last three legacy importers
(``chainlit_api.py``, the ``components/auth`` test module, and the
``tests/integration/api/test_oidc_lifecycle.py`` fixture) onto this module
and deleted the ``components/auth/middleware.py`` shim.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from core.auth.chainlit import CHAINLIT_TOKEN_COOKIE_NAME
from core.config.auth import AuthBypassConfig
from core.utils.logging import get_logger
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from limits import parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter
from starlette.middleware.base import BaseHTTPMiddleware

logger = get_logger()


SESSION_COOKIE_NAME = "openrag_session"


# Default policy used by the module-level helpers below. The path lists
# themselves live on :class:`core.config.auth.AuthBypassConfig` so the
# Phase 10C "config-driven bypass paths" rule is satisfied; callers that
# need a tighter policy hand a custom :class:`AuthBypassConfig` to
# :class:`AuthMiddleware` at registration time.
_DEFAULT_BYPASS_CONFIG = AuthBypassConfig()


def is_ui_path(path: str, *, bypass_config: AuthBypassConfig | None = None) -> bool:
    """True if this path is a browser-facing page (UI), not an API route.

    Used only in ``AUTH_MODE=oidc`` to decide between a 302 redirect to
    ``/auth/login`` (UI) and a 401 JSON response (API). When in doubt we
    return ``False`` to avoid redirect loops on non-browser clients.
    """
    cfg = bypass_config or _DEFAULT_BYPASS_CONFIG
    if path == "/":
        return True
    if any(path.startswith(p) for p in cfg.api_prefixes):
        return False
    if any(path.startswith(p) for p in cfg.ui_path_prefixes):
        return True
    return False


def is_bypass_path(path: str, *, bypass_config: AuthBypassConfig | None = None) -> bool:
    """True if ``AuthMiddleware`` should let the request through without
    consulting the auth service.

    Matches a literal bypass list (``/docs``, health probes, OIDC
    callback endpoints) plus the entire ``/chainlit`` subtree — Chainlit
    handles its own header-auth callback for those routes.

    ``/assets/`` is also bypassed: Chainlit is submounted at ``/chainlit`` but
    its bundled pdf.js worker requests itself from the origin-root
    ``/assets/pdf.worker*.mjs`` (a runtime-computed URL the HTML root-path
    rewrite never sees). Those files are public frontend bundles — the same
    ones already served (and bypassed) under ``/chainlit/assets`` — so serving
    them at ``/assets`` lets the module worker load with a JS MIME type instead
    of a 403 JSON body that breaks source PDF previews.
    """
    cfg = bypass_config or _DEFAULT_BYPASS_CONFIG
    return path in cfg.bypass_paths or path == "/chainlit" or path.startswith(("/chainlit/", "/assets/"))


def _allow_no_auth() -> bool:
    """Whether the no-auth dev bypass (AUTH_TOKEN unset → admin) is allowed.

    Read lazily so it can be toggled per-test, mirroring the other env reads.
    """
    return os.getenv("ALLOW_NO_AUTH", "false").strip().lower() == "true"


def _env_flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class AuthFailureRateLimiter:
    """Limit failed authentication attempts by client IP."""

    def __init__(self) -> None:
        self.enabled = _env_flag("RATE_LIMIT_ENABLED", True)
        self._limiter = MovingWindowRateLimiter(MemoryStorage())
        self._limit = (
            parse(os.environ.get("RATE_LIMIT_AUTH_FAILURE", os.environ.get("RATE_LIMIT_AUTH", "20/minute")))
            if self.enabled
            else None
        )

    @staticmethod
    def _identity(request: Request) -> str:
        client = request.client
        return f"ip:{client.host}" if client else "ip:unknown"

    @staticmethod
    def _safe_log_value(value: str, *, max_len: int = 200) -> str:
        return value.replace("\r", "\\r").replace("\n", "\\n").replace("\x00", "\\0")[:max_len]

    async def response_if_limited(self, request: Request) -> JSONResponse | None:
        if not self.enabled or self._limit is None:
            return None
        identity = self._identity(request)
        tier = "auth-failure"
        allowed = await self._limiter.test(self._limit, tier, identity)
        if allowed:
            return None
        return await self._limited_response(request, identity)

    async def record_failure(self, request: Request) -> JSONResponse | None:
        if not self.enabled or self._limit is None:
            return None
        identity = self._identity(request)
        tier = "auth-failure"
        allowed = await self._limiter.hit(self._limit, tier, identity)
        if allowed:
            return None
        return await self._limited_response(request, identity)

    async def _limited_response(self, request: Request, identity: str) -> JSONResponse:
        assert self._limit is not None
        tier = "auth-failure"
        stats = await self._limiter.get_window_stats(self._limit, tier, identity)
        retry_after = max(1, int(stats.reset_time - time.time()))
        logger.bind(
            path=self._safe_log_value(str(request.url.path)),
            identity=self._safe_log_value(identity),
        ).warning("Auth failure rate limit exceeded")
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please retry later.", "extra": {}},
            headers={"Retry-After": str(retry_after)},
        )


class AuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware enforcing authentication for both token and oidc modes.

    Constructor takes a ``get_auth_service`` callable so tests can inject a
    fake service and the live app can resolve the request-time container.
    An optional :class:`AuthBypassConfig` overrides which paths bypass
    the auth check; the default matches the legacy hardcoded sets.
    """

    def __init__(
        self,
        app,
        *,
        get_auth_service: Callable[[Request], Any],
        bypass_config: AuthBypassConfig | None = None,
    ):
        super().__init__(app)
        self._get_auth_service = get_auth_service
        self._bypass_config = bypass_config or AuthBypassConfig()
        self._auth_failure_limiter = AuthFailureRateLimiter()

    async def _auth_failure(self, request: Request, *, status_code: int, detail: str) -> JSONResponse:
        limited = await self._auth_failure_limiter.record_failure(request)
        if limited is not None:
            return limited
        return JSONResponse(status_code=status_code, content={"detail": detail})

    async def dispatch(self, request: Request, call_next):
        # Read env lazily so tests can flip AUTH_MODE per-test.
        auth_mode = os.getenv("AUTH_MODE", "token").strip().lower()
        auth_token = os.getenv("AUTH_TOKEN")
        enc_key = os.getenv("OIDC_TOKEN_ENCRYPTION_KEY") or ""

        # --- Dev mode: AUTH_MODE=token + AUTH_TOKEN unset → user 1 bypass.
        #     This disables authentication entirely (every request becomes the
        #     admin user), so it is gated behind an explicit ALLOW_NO_AUTH=true
        #     opt-in. Without that flag, an unset AUTH_TOKEN does not grant
        #     access: requests fall through to normal Bearer auth and
        #     unauthenticated callers get 401.
        if auth_mode == "token" and auth_token is None and _allow_no_auth():
            logger.warning(
                "ALLOW_NO_AUTH=true and AUTH_TOKEN is unset: authentication is DISABLED — "
                "every request is treated as admin user 1. Never use this in production."
            )
            auth_service = self._get_auth_service(request)
            user = await auth_service.get_user_for_request(1)
            user_partitions = await auth_service.list_user_partitions_for_request(1)
            request.state.user = user
            request.state.user_partitions = user_partitions
            request.state.oidc_session = None
            return await call_next(request)

        # --- Bypass list (docs, health, /auth/* callbacks, chainlit).
        path = request.url.path

        # In oidc mode the interactive API docs are gated behind login rather
        # than served anonymously: they expose the full route + schema surface,
        # and an authenticated browser session still reaches them after the IdP
        # redirect. In token mode they stay public — a browser has no login flow
        # to bounce through — preserving the legacy bypass contract. ``oidc_gated``
        # therefore suppresses the bypass for these paths only when AUTH_MODE=oidc,
        # so they fall through to the normal auth + UI-redirect path below.
        oidc_gated = auth_mode == "oidc" and path in self._bypass_config.oidc_gated_paths

        if not oidc_gated and is_bypass_path(path, bypass_config=self._bypass_config):
            # Special case: browser HTML page-loads on /chainlit/* without an
            # active session can't be served usefully — Chainlit configures no
            # in-app auth provider when headerAuth is used, so the SPA shows
            # a dead-end "Login to access the app" screen with no actionable
            # button. Redirect those to /auth/login so the OIDC flow takes
            # over. API/asset/WebSocket requests (Accept != text/html) keep
            # the bypass so Chainlit's own headerAuth callback can validate.
            if (
                auth_mode == "oidc"
                and path.startswith("/chainlit")
                and "text/html" in request.headers.get("accept", "").lower()
                and not request.headers.get("authorization", "").lower().startswith("bearer ")
            ):
                cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
                session_valid = False
                if cookie_token:
                    # Never let a session-lookup failure (e.g. a degraded boot
                    # where the container's pool was never opened) surface as a
                    # 500 on a plain page load — treat it as no session and let
                    # the redirect below send the browser through /auth/login.
                    try:
                        auth_service = self._get_auth_service(request)
                        session = await auth_service.get_oidc_session_by_token_for_request(cookie_token)
                        session_valid = session is not None
                    except Exception as e:
                        logger.bind(error=str(e)).warning(
                            "OIDC session check failed on chainlit bypass; redirecting to login"
                        )
                        session_valid = False
                if not session_valid:
                    chainlit_token = request.cookies.get(CHAINLIT_TOKEN_COOKIE_NAME)
                    if chainlit_token:
                        try:
                            auth_service = self._get_auth_service(request)
                            user = await auth_service.get_user_by_token_for_request(chainlit_token)
                            session_valid = user is not None
                        except Exception as e:
                            logger.bind(error=str(e)).warning(
                                "Chainlit token handoff check failed; redirecting to login"
                            )
                            session_valid = False
                if not session_valid:
                    next_path = path
                    if request.url.query:
                        next_path = f"{path}?{request.url.query}"
                    return RedirectResponse(
                        url=f"/auth/login?next={quote(next_path, safe='')}",
                        status_code=302,
                    )
            return await call_next(request)

        limited = await self._auth_failure_limiter.response_if_limited(request)
        if limited is not None:
            return limited

        user = None
        session = None
        try:
            auth_service = self._get_auth_service(request)
        except RuntimeError:
            logger.warning("Auth service unavailable", reason="service_unavailable", path=request.url.path)
            return JSONResponse(status_code=503, content={"detail": "Service unavailable"})

        # --- 1) Cookie session (OIDC UI flow). Gated on oidc mode so the
        #        legacy token-mode contract remains strictly Bearer-only —
        #        a stray openrag_session cookie must not authenticate a
        #        request when AUTH_MODE=token.
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME) if auth_mode == "oidc" else None
        if cookie_token:
            session = await auth_service.get_oidc_session_by_token_for_request(cookie_token)
            if session is not None:
                refreshed = await auth_service.refresh_session_if_needed(
                    session=session,
                    enc_key=enc_key,
                )
                if refreshed is None:
                    # Refresh failed or session unusable → revoke and fall through.
                    try:
                        await auth_service.revoke_oidc_session_by_id_for_request(session["id"])
                    except Exception as e:
                        logger.bind(error=str(e)).warning("Failed to revoke invalid OIDC session")
                    session = None
                else:
                    session = refreshed
                    user = await auth_service.get_user_for_request(session["user_id"])

        # --- 2) Fallback: Bearer / ?token= (programmatic clients + internal
        #        callers like Chainlit's header_auth_callback which forwards
        #        the browser cookie value in the Authorization header).
        if user is None:
            token = None
            if path.startswith("/static"):
                token = getattr(request.state, "original_token", None)
            else:
                auth = request.headers.get("authorization", "")
                if auth and auth.lower().startswith("bearer "):
                    token = auth.split(" ", 1)[1]

            if token is not None:
                # In oidc mode, a Bearer may carry an OIDC session token (not
                # a ``users.token`` hash). Try the session lookup first with
                # the same lazy-refresh semantics as the cookie branch above.
                if auth_mode == "oidc":
                    session = await auth_service.get_oidc_session_by_token_for_request(token)
                    if session is not None:
                        refreshed = await auth_service.refresh_session_if_needed(
                            session=session,
                            enc_key=enc_key,
                        )
                        if refreshed is None:
                            try:
                                await auth_service.revoke_oidc_session_by_id_for_request(session["id"])
                            except Exception as e:
                                logger.bind(error=str(e)).warning("Failed to revoke invalid OIDC session (bearer path)")
                            session = None
                        else:
                            session = refreshed
                            user = await auth_service.get_user_for_request(session["user_id"])

                if user is None:
                    # Either token mode, or oidc mode with no matching session
                    # — fall back to the long-lived ``users.token`` used by
                    # programmatic clients (CI, scripts, service agents).
                    user = await auth_service.get_user_by_token_for_request(token)
                    if not user and auth_mode == "token":
                        # Legacy test contract: robot suite asserts 403 + "Invalid token".
                        return await self._auth_failure(request, status_code=403, detail="Invalid token")
            elif auth_mode == "token":
                # Token mode: no cookie + no bearer → legacy 403 "Missing token".
                return await self._auth_failure(request, status_code=403, detail="Missing token")

        # --- 3) Unauthenticated: redirect UI (and the oidc-gated docs) in oidc
        #        mode, else 401 JSON. ``oidc_gated`` is already mode-checked.
        if user is None:
            if oidc_gated or (auth_mode == "oidc" and is_ui_path(path, bypass_config=self._bypass_config)):
                next_path = path
                if request.url.query:
                    next_path = f"{path}?{request.url.query}"
                return RedirectResponse(
                    url=f"/auth/login?next={quote(next_path, safe='')}",
                    status_code=302,
                )
            return await self._auth_failure(request, status_code=401, detail="Unauthenticated")

        # --- Happy path: user resolved.
        request.state.user = user
        request.state.user_partitions = await auth_service.list_user_partitions_for_request(user["id"])
        request.state.oidc_session = session  # None when authenticated via Bearer
        return await call_next(request)


__all__ = [
    "AuthMiddleware",
    "SESSION_COOKIE_NAME",
    "is_bypass_path",
    "is_ui_path",
]
