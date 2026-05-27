"""OIDC authentication routes — thin HTTP layer over :class:`AuthService`.

Routes exposed (all bypassed by ``AuthMiddleware``):
  - ``GET  /auth/login``              — start Authorization Code + PKCE flow
  - ``GET  /auth/callback``           — handle IdP redirect, create session
  - ``POST /auth/backchannel-logout`` — IdP-driven session revocation (OIDC spec)
  - ``GET  /auth/logout``             — RP-initiated logout (local + IdP)

One more route sits *behind* the middleware:
  - ``GET  /auth/me``                 — debug endpoint returning the current user.

All routes return ``400`` when ``AUTH_MODE != "oidc"`` — the feature is dormant
in ``token`` mode.

Phase 8A.1: every business decision (PKCE/state generation, code exchange,
user lookup / provisioning, session creation, logout-URL construction) now
lives in :class:`services.orchestrators.auth_service.AuthService`. This
module only does HTTP transport: the ``AUTH_MODE`` gate, cookie set/clear,
the Secure-flag heuristic, and mapping :class:`OIDCFlowError` to responses.
"""

from __future__ import annotations

import os

from components.auth import StateCookieSerializer
from di.providers import get_auth_service
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from services.orchestrators.auth_service import (
    SESSION_COOKIE_NAME,
    AuthService,
    OIDCFlowError,
)
from utils.logger import get_logger

logger = get_logger()
router = APIRouter()


# ---------------------------------------------------------------------------
# HTTP-transport helpers (kept in the router by design)
# ---------------------------------------------------------------------------


def _auth_mode() -> str:
    return os.getenv("AUTH_MODE", "token").strip().lower()


def _require_oidc_mode() -> None:
    if _auth_mode() != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AUTH_MODE is not 'oidc' — authentication routes are disabled.",
        )


def _is_request_secure(request: Request) -> bool:
    """True if the client-observed scheme is HTTPS.

    Checks ``PREFERRED_URL_SCHEME``, the ``X-Forwarded-Proto`` header
    (client-most hop when comma-separated), then ``request.url.scheme``.
    """
    if os.environ.get("PREFERRED_URL_SCHEME", "").lower() == "https":
        return True
    xfp = request.headers.get("x-forwarded-proto", "")
    if xfp.split(",", 1)[0].strip().lower() == "https":
        return True
    return request.url.scheme == "https"


def _delete_state_cookie(response: Response) -> None:
    response.delete_cookie(key=StateCookieSerializer.COOKIE_NAME, path="/")


def _json_error(status_code: int, detail: str, *, delete_state_cookie: bool = False) -> JSONResponse:
    r = JSONResponse(status_code=status_code, content={"detail": detail})
    if delete_state_cookie:
        _delete_state_cookie(r)
    return r


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------


@router.get("/auth/login", include_in_schema=False)
async def login(
    request: Request,
    next: str | None = None,
    _oidc: None = Depends(_require_oidc_mode),
    service: AuthService = Depends(get_auth_service),
):
    try:
        result = await service.start_oidc_login(next)
    except OIDCFlowError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e

    response = RedirectResponse(url=result.authorization_url, status_code=302)
    response.set_cookie(
        key=result.state_cookie_name,
        value=result.state_cookie_value,
        max_age=result.state_cookie_max_age,
        httponly=True,
        secure=_is_request_secure(request),
        samesite="lax",
        path="/",
    )
    return response


# ---------------------------------------------------------------------------
# GET /auth/callback
# ---------------------------------------------------------------------------


@router.get("/auth/callback", include_in_schema=False)
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    _oidc: None = Depends(_require_oidc_mode),
    service: AuthService = Depends(get_auth_service),
):
    try:
        result = await service.handle_oidc_callback(
            code=code,
            state=state,
            state_cookie_raw=request.cookies.get(StateCookieSerializer.COOKIE_NAME),
        )
    except OIDCFlowError as e:
        return _json_error(e.status_code, e.message, delete_state_cookie=True)

    redirect = RedirectResponse(url=result.next_url, status_code=302)
    _delete_state_cookie(redirect)
    redirect.set_cookie(
        key=result.session_cookie_name,
        value=result.session_cookie_value,
        max_age=result.session_cookie_max_age,
        httponly=True,
        secure=_is_request_secure(request),
        samesite="lax",
        path="/",
    )
    return redirect


# ---------------------------------------------------------------------------
# POST /auth/backchannel-logout
# ---------------------------------------------------------------------------


@router.post("/auth/backchannel-logout", include_in_schema=False)
async def backchannel_logout(
    logout_token: str = Form(...),
    _oidc: None = Depends(_require_oidc_mode),
    service: AuthService = Depends(get_auth_service),
):
    """IdP-initiated logout per OIDC Back-Channel Logout spec.

    Content-Type: ``application/x-www-form-urlencoded`` with field ``logout_token``.
    """
    try:
        await service.handle_backchannel_logout(logout_token)
    except OIDCFlowError as e:
        content: dict[str, str] = {"error": "invalid_request"}
        if e.error_description:
            content["error_description"] = e.error_description
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=content,
            headers={"Cache-Control": "no-store"},
        )

    return Response(
        status_code=status.HTTP_200_OK,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# GET /auth/logout
# ---------------------------------------------------------------------------


@router.get("/auth/logout", include_in_schema=False)
async def logout(
    request: Request,
    _oidc: None = Depends(_require_oidc_mode),
    service: AuthService = Depends(get_auth_service),
):
    redirect_target = await service.logout(request.cookies.get(SESSION_COOKIE_NAME))

    if redirect_target:
        response: Response = RedirectResponse(url=redirect_target, status_code=302)
    else:
        # No IdP end_session and no local post-logout URL → confirm the
        # logout in-place. The cookie deletion below still takes effect.
        response = JSONResponse(status_code=200, content={"detail": "Logged out"})
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# GET /auth/me  — standard AuthMiddleware applies (route NOT in bypass list)
# ---------------------------------------------------------------------------


@router.get("/auth/me")
async def me(request: Request):
    """Debug/health endpoint — returns the user bound by AuthMiddleware."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authenticated user on request.state",
        )
    oidc_session = getattr(request.state, "oidc_session", None)
    session_expires_at = None
    if oidc_session and oidc_session.get("session_expires_at"):
        exp = oidc_session["session_expires_at"]
        try:
            session_expires_at = exp.isoformat()
        except AttributeError:
            session_expires_at = str(exp)

    return {
        "user_id": user.get("id"),
        "email": user.get("email"),
        "auth_method": "oidc" if oidc_session else "token",
        "session_expires_at": session_expires_at,
    }
