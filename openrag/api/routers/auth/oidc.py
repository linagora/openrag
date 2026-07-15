"""OIDC authentication routes — thin HTTP layer over :class:`AuthService`.

Routes exposed (all bypassed by ``AuthMiddleware``):
  - ``GET  /auth/login``              — start Authorization Code + PKCE flow
  - ``GET  /auth/callback``           — handle IdP redirect, create session
  - ``POST /auth/backchannel-logout`` — IdP-driven session revocation (OIDC spec)
  - ``GET  /auth/logout``             — RP-initiated logout (local + IdP)

One more route sits *behind* the middleware:
  - ``GET  /auth/me``                 — debug endpoint returning the current user.
  - ``POST /auth/chainlit-session``   — short-lived Chainlit handoff for token users.

The OIDC flow routes return ``400`` when ``AUTH_MODE != "oidc"``. The
middleware-protected utility routes stay available to authenticated users in
both modes.

Phase 8A.1: every business decision (PKCE/state generation, code exchange,
user lookup / provisioning, session creation, logout-URL construction) now
lives in :class:`services.orchestrators.auth_service.AuthService`. This
module only does HTTP transport: the ``AUTH_MODE`` gate, cookie set/clear,
the Secure-flag heuristic, and mapping :class:`OIDCFlowError` to responses.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from api.dependencies.auth import current_user
from core.auth.chainlit import (
    CHAINLIT_AUTH_COOKIE_NAME,
    CHAINLIT_TOKEN_COOKIE_MAX_AGE_SECONDS,
    CHAINLIT_TOKEN_COOKIE_NAME,
    CHAINLIT_TOKEN_COOKIE_PATH,
)
from core.auth.state_cookie import StateCookieSerializer
from core.utils.exceptions import OpenRAGError
from core.utils.logging import get_logger
from di.providers import get_auth_service
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

logger = get_logger()
router = APIRouter()
SESSION_COOKIE_NAME = "openrag_session"


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


def _is_cross_origin_request(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    origin_host = urlparse(origin).hostname
    request_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    request_host = urlparse(f"//{request_host}").hostname
    return bool(origin_host and request_host and origin_host != request_host)


def _chainlit_handoff_cookie_samesite(request: Request) -> str:
    if _is_request_secure(request) and _is_cross_origin_request(request):
        return "none"
    return "lax"


def _delete_state_cookie(response: Response) -> None:
    response.delete_cookie(key=StateCookieSerializer.COOKIE_NAME, path="/")


def _clear_chainlit_auth_cookie(request: Request, response: Response) -> None:
    cookie_names = {
        CHAINLIT_AUTH_COOKIE_NAME,
        *(name for name in request.cookies if name.startswith(f"{CHAINLIT_AUTH_COOKIE_NAME}_")),
    }
    for cookie_name in cookie_names:
        response.delete_cookie(key=cookie_name, path="/")
        response.delete_cookie(key=cookie_name, path=CHAINLIT_TOKEN_COOKIE_PATH)


def _json_error(status_code: int, detail: str, *, delete_state_cookie: bool = False) -> JSONResponse:
    r = JSONResponse(status_code=status_code, content={"detail": detail})
    if delete_state_cookie:
        _delete_state_cookie(r)
    return r


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------


@router.get("/auth/login", include_in_schema=False)
async def login(
    request: Request,
    next: str | None = None,
    _oidc: None = Depends(_require_oidc_mode),
    service=Depends(get_auth_service),
):
    try:
        result = await service.start_oidc_login(next)
    except OpenRAGError as e:
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
    service=Depends(get_auth_service),
):
    try:
        result = await service.handle_oidc_callback(
            code=code,
            state=state,
            state_cookie_raw=request.cookies.get(StateCookieSerializer.COOKIE_NAME),
        )
    except OpenRAGError as e:
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
    service=Depends(get_auth_service),
):
    """IdP-initiated logout per OIDC Back-Channel Logout spec.

    Content-Type: ``application/x-www-form-urlencoded`` with field ``logout_token``.
    """
    try:
        await service.handle_backchannel_logout(logout_token)
    except OpenRAGError as e:
        content: dict[str, str] = {"error": "invalid_request"}
        error_description = getattr(e, "error_description", None)
        if error_description:
            content["error_description"] = error_description
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


def _is_csrf_safe_navigation(request: Request) -> bool:
    """Reject the silent logout-CSRF vector while keeping the redirect-based UX.

    Logout must remain reachable via top-level GET navigation (the OIDC
    RP-initiated logout redirects the browser to the IdP), so we can't simply
    require POST. Instead we use the Fetch Metadata headers: a forged request
    from ``<img>``/``<script>``/cross-site ``fetch`` is a cross-site
    *non-navigation* request and is blocked; genuine top-level navigations
    (Sec-Fetch-Mode: navigate) and same-origin requests pass. Browsers that
    don't send these headers (older) fall through as allowed.
    """
    site = request.headers.get("sec-fetch-site")
    mode = request.headers.get("sec-fetch-mode")
    if site == "cross-site" and mode not in (None, "navigate"):
        return False
    return True


@router.get("/auth/logout", include_in_schema=False)
async def logout(
    request: Request,
    _oidc: None = Depends(_require_oidc_mode),
    service=Depends(get_auth_service),
):
    if not _is_csrf_safe_navigation(request):
        logger.warning("Blocked cross-site non-navigation request to /auth/logout (CSRF)")
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "Cross-site logout requests are not allowed"},
        )

    redirect_target = await service.logout(request.cookies.get(SESSION_COOKIE_NAME))

    if redirect_target:
        response: Response = RedirectResponse(url=redirect_target, status_code=302)
    else:
        # No IdP end_session and no local post-logout URL → confirm the
        # logout in-place. The cookie deletion below still takes effect.
        response = JSONResponse(status_code=200, content={"detail": "Logged out"})
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=CHAINLIT_TOKEN_COOKIE_NAME, path=CHAINLIT_TOKEN_COOKIE_PATH)
    return response


# ---------------------------------------------------------------------------
# POST /auth/chainlit-session  — standard AuthMiddleware applies
# ---------------------------------------------------------------------------


@router.post("/auth/chainlit-session", include_in_schema=False)
async def chainlit_session(request: Request, _user=Depends(current_user)):
    """Prepare a short-lived Chainlit handoff for bearer-token users.

    OIDC browser users already carry the ``openrag_session`` cookie to
    ``/chainlit/``. Bearer-token users do not, so the Admin UI calls this route
    before opening Chat. The route is protected by AuthMiddleware and stores the
    already-validated bearer only in a short-lived, HTTP-only cookie scoped to
    the Chainlit path.
    """

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_chainlit_auth_cookie(request, response)
    token = _bearer_token(request)
    if not token or getattr(request.state, "oidc_session", None) is not None:
        return response

    response.set_cookie(
        key=CHAINLIT_TOKEN_COOKIE_NAME,
        value=token,
        max_age=CHAINLIT_TOKEN_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=_is_request_secure(request),
        samesite=_chainlit_handoff_cookie_samesite(request),
        path=CHAINLIT_TOKEN_COOKIE_PATH,
    )
    return response


@router.delete("/auth/chainlit-session", include_in_schema=False)
async def clear_chainlit_session(request: Request, _user=Depends(current_user)):
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(key=CHAINLIT_TOKEN_COOKIE_NAME, path=CHAINLIT_TOKEN_COOKIE_PATH)
    _clear_chainlit_auth_cookie(request, response)
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
