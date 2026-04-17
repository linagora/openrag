"""Auth middleware — Phase 5 of the OIDC integration.

Extracted from ``openrag/api.py`` so the dispatch logic can be unit tested
without importing the full application (which bootstraps Ray actors at
import time).

The middleware supports both authentication modes:

  * ``AUTH_MODE=token`` (legacy) — single Bearer token from
    ``Authorization: Bearer ...`` or ``?token=`` for ``/static``. On missing
    or invalid token returns **403** with the legacy JSON body (the existing
    Robot Framework suite asserts this shape).

  * ``AUTH_MODE=oidc`` — cookie-based session (set by the OIDC callback),
    with lazy access-token refresh. A Bearer token is still accepted as a
    fallback for programmatic clients. On missing/invalid credentials:

      - UI paths → **302** to ``/auth/login?next=<encoded>``
      - API paths → **401** JSON ``{"detail": "Unauthenticated"}``

Environment configuration (``AUTH_MODE``, ``AUTH_TOKEN``,
``OIDC_TOKEN_ENCRYPTION_KEY``) is read via ``os.getenv`` at **dispatch** time,
not at import time, so tests can monkeypatch ``os.environ``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from urllib.parse import quote

from components.auth.refresh import refresh_session_if_needed
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from utils.logger import get_logger

logger = get_logger()


SESSION_COOKIE_NAME = "openrag_session"


_BYPASS_PATHS = frozenset(
    {
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
)

# Paths that are part of the REST API — unauthenticated requests here must
# get JSON 401/403, never an HTML redirect to /auth/login (which would break
# programmatic clients that follow redirects).
_API_PREFIXES = (
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
)

# Browser-facing paths — unauthenticated access in oidc mode → 302 /auth/login.
_UI_PATH_PREFIXES = ("/static",)


def is_ui_path(path: str) -> bool:
    """True if this path is a browser-facing page (UI), not an API route.

    Used only in ``AUTH_MODE=oidc`` to decide between a 302 redirect to
    ``/auth/login`` (UI) and a 401 JSON response (API). When in doubt we
    return ``False`` to avoid redirect loops on non-browser clients.
    """
    if path == "/":
        return True
    if any(path.startswith(p) for p in _API_PREFIXES):
        return False
    if any(path.startswith(p) for p in _UI_PATH_PREFIXES):
        return True
    return False


def is_bypass_path(path: str) -> bool:
    return path in _BYPASS_PATHS or path.startswith("/chainlit")


class AuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware enforcing authentication for both token and oidc modes.

    Constructor takes a ``get_vectordb`` callable returning the Ray actor
    handle — this indirection keeps the middleware decoupled from
    ``utils.dependencies`` so tests can inject a ``MagicMock``.
    """

    def __init__(self, app, *, get_vectordb: Callable[[], object]):
        super().__init__(app)
        self._get_vectordb = get_vectordb

    async def dispatch(self, request: Request, call_next):
        # Read env lazily so tests can flip AUTH_MODE per-test.
        auth_mode = os.getenv("AUTH_MODE", "token").strip().lower()
        auth_token = os.getenv("AUTH_TOKEN")
        enc_key = os.getenv("OIDC_TOKEN_ENCRYPTION_KEY") or ""

        vectordb = self._get_vectordb()

        # --- Dev mode: AUTH_MODE=token + AUTH_TOKEN unset → user 1 bypass.
        if auth_mode == "token" and auth_token is None:
            user = await vectordb.get_user.remote(1)
            user_partitions = await vectordb.list_user_partitions.remote(1)
            request.state.user = user
            request.state.user_partitions = user_partitions
            request.state.oidc_session = None
            return await call_next(request)

        # --- Bypass list (docs, health, /auth/* callbacks, chainlit).
        path = request.url.path
        if is_bypass_path(path):
            return await call_next(request)

        user = None
        session = None

        # --- 1) Cookie session (OIDC UI flow).
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie_token:
            session = await vectordb.get_oidc_session_by_token.remote(cookie_token)
            if session is not None:
                refreshed = await refresh_session_if_needed(
                    session=session,
                    enc_key=enc_key,
                    vectordb=vectordb,
                )
                if refreshed is None:
                    # Refresh failed or session unusable → revoke and fall through.
                    try:
                        await vectordb.revoke_oidc_session_by_id.remote(session["id"])
                    except Exception as e:
                        logger.bind(error=str(e)).warning("Failed to revoke invalid OIDC session")
                    session = None
                else:
                    session = refreshed
                    user = await vectordb.get_user.remote(session["user_id"])

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
                    session = await vectordb.get_oidc_session_by_token.remote(token)
                    if session is not None:
                        refreshed = await refresh_session_if_needed(
                            session=session,
                            enc_key=enc_key,
                            vectordb=vectordb,
                        )
                        if refreshed is None:
                            try:
                                await vectordb.revoke_oidc_session_by_id.remote(session["id"])
                            except Exception as e:
                                logger.bind(error=str(e)).warning("Failed to revoke invalid OIDC session (bearer path)")
                            session = None
                        else:
                            session = refreshed
                            user = await vectordb.get_user.remote(session["user_id"])

                if user is None:
                    # Either token mode, or oidc mode with no matching session
                    # — fall back to the long-lived ``users.token`` used by
                    # programmatic clients (CI, scripts, service agents).
                    user = await vectordb.get_user_by_token.remote(token)
                    if not user and auth_mode == "token":
                        # Legacy test contract: robot suite asserts 403 + "Invalid token".
                        return JSONResponse(status_code=403, content={"detail": "Invalid token"})
            elif auth_mode == "token":
                # Token mode: no cookie + no bearer → legacy 403 "Missing token".
                return JSONResponse(status_code=403, content={"detail": "Missing token"})

        # --- 3) Unauthenticated: redirect UI in oidc mode, else 401 JSON.
        if user is None:
            if auth_mode == "oidc" and is_ui_path(path):
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
        request.state.user_partitions = await vectordb.list_user_partitions.remote(user["id"])
        request.state.oidc_session = session  # None when authenticated via Bearer
        return await call_next(request)
