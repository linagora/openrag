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
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from core.config.auth import AuthBypassConfig
from core.utils.logging import get_logger
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
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
    """
    cfg = bypass_config or _DEFAULT_BYPASS_CONFIG
    return path in cfg.bypass_paths or path == "/chainlit" or path.startswith("/chainlit/")


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

    async def dispatch(self, request: Request, call_next):
        # Read env lazily so tests can flip AUTH_MODE per-test.
        auth_mode = os.getenv("AUTH_MODE", "token").strip().lower()
        auth_token = os.getenv("AUTH_TOKEN")
        enc_key = os.getenv("OIDC_TOKEN_ENCRYPTION_KEY") or ""

        # --- Dev mode: AUTH_MODE=token + AUTH_TOKEN unset → user 1 bypass.
        if auth_mode == "token" and auth_token is None:
            auth_service = self._get_auth_service(request)
            user = await auth_service.get_user_for_request(1)
            user_partitions = await auth_service.list_user_partitions_for_request(1)
            request.state.user = user
            request.state.user_partitions = user_partitions
            request.state.oidc_session = None
            return await call_next(request)

        # --- Bypass list (docs, health, /auth/* callbacks, chainlit).
        path = request.url.path
        if is_bypass_path(path, bypass_config=self._bypass_config):
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
                    auth_service = self._get_auth_service(request)
                    session = await auth_service.get_oidc_session_by_token_for_request(cookie_token)
                    session_valid = session is not None
                if not session_valid:
                    next_path = path
                    if request.url.query:
                        next_path = f"{path}?{request.url.query}"
                    return RedirectResponse(
                        url=f"/auth/login?next={quote(next_path, safe='')}",
                        status_code=302,
                    )
            return await call_next(request)

        user = None
        session = None
        try:
            auth_service = self._get_auth_service(request)
        except Exception as exc:
            logger.warning("Auth service unavailable", error_type=type(exc).__name__, path=request.url.path)
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
                        return JSONResponse(status_code=403, content={"detail": "Invalid token"})
            elif auth_mode == "token":
                # Token mode: no cookie + no bearer → legacy 403 "Missing token".
                return JSONResponse(status_code=403, content={"detail": "Missing token"})

        # --- 3) Unauthenticated: redirect UI in oidc mode, else 401 JSON.
        if user is None:
            if auth_mode == "oidc" and is_ui_path(path, bypass_config=self._bypass_config):
                next_path = path
                if request.url.query:
                    next_path = f"{path}?{request.url.query}"
                return RedirectResponse(
                    url=f"/auth/login?next={quote(next_path, safe='')}",
                    status_code=302,
                )
            return JSONResponse(status_code=401, content={"detail": "Unauthenticated"})

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
