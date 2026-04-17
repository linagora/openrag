"""OIDC authentication routes — phase 4 of the OIDC integration.

Routes exposed (all bypassed by ``AuthMiddleware``):
  - ``GET  /auth/login``              — start Authorization Code + PKCE flow
  - ``GET  /auth/callback``           — handle IdP redirect, create session
  - ``POST /auth/backchannel-logout`` — IdP-driven session revocation (OIDC spec)
  - ``GET  /auth/logout``             — RP-initiated logout (local + IdP)

One more route sits *behind* the middleware:
  - ``GET  /auth/me``                 — debug endpoint returning the current user.

All routes return ``400`` when ``AUTH_MODE != "oidc"`` — the feature is dormant
in ``token`` mode.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from components.auth import (
    OIDCClient,
    StateCookiePayload,
    StateCookieSerializer,
    decrypt_token,
    encrypt_token,
    get_oidc_client,
    issue_session_token,
)
from utils.dependencies import get_vectordb
from utils.logger import get_logger

logger = get_logger()
router = APIRouter()


SESSION_COOKIE_NAME = "openrag_session"


# ---------------------------------------------------------------------------
# Env helpers — read lazily so tests can monkeypatch os.environ
# ---------------------------------------------------------------------------

def _auth_mode() -> str:
    return os.getenv("AUTH_MODE", "token").strip().lower()


def _token_encryption_key() -> str:
    key = os.getenv("OIDC_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("OIDC_TOKEN_ENCRYPTION_KEY is not set")
    return key


def _email_source() -> str:
    return os.getenv("OIDC_EMAIL_SOURCE", "id_token").strip().lower()


def _allowed_email_domains() -> list[str]:
    raw = os.getenv("OIDC_ALLOWED_EMAIL_DOMAINS", "").strip()
    if not raw:
        return []
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _post_logout_redirect_uri() -> str:
    return os.getenv("OIDC_POST_LOGOUT_REDIRECT_URI", "/")


def _oidc_client_id() -> str:
    return os.environ["OIDC_CLIENT_ID"]


def _require_oidc_mode():
    if _auth_mode() != "oidc":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AUTH_MODE is not 'oidc' — authentication routes are disabled.",
        )


def _is_request_secure(request: Request) -> bool:
    """True if the client-observed scheme is HTTPS.

    ``request.url.scheme`` already accounts for reverse-proxy headers when the
    app is started with ``proxy_headers=True`` (see ``api.py``).
    """
    return request.url.scheme == "https"


def _state_serializer() -> StateCookieSerializer:
    return StateCookieSerializer(secret_key=_token_encryption_key())


def _allowed_next_origins() -> set[str]:
    """Origins (scheme://host[:port]) accepted as redirect targets after login.

    Mirrors the CORS allow_origins from ``api.py``: localhost dev ports plus
    ``INDEXERUI_URL`` so that the indexer-ui (served on a different port) can
    receive the user back after the OIDC flow completes.
    """
    origins = {"http://localhost:3042", "http://localhost:5173"}
    indexer_ui = os.getenv("INDEXERUI_URL")
    if indexer_ui:
        origins.add(indexer_ui.rstrip("/"))
    return origins


def _sanitize_next_url(next_url: str | None) -> str:
    """Accept either a same-origin relative path (``/...`` but not ``//...``)
    or an absolute URL whose origin is explicitly whitelisted (indexer-ui,
    dev-only localhost). Fall back to ``/`` on any mismatch — protects against
    open-redirect attacks.
    """
    if not next_url:
        return "/"
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    # Absolute URL: only allow whitelisted origins.
    parsed = urlparse(next_url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in _allowed_next_origins():
            return next_url
    return "/"


def _utcnow() -> datetime:
    # DB-side timestamps are naive local time (models' default is ``datetime.now``),
    # and every read site compares against ``datetime.now()``. Using ``datetime.now()``
    # here keeps newly-issued sessions from appearing pre-expired on non-UTC hosts.
    return datetime.now()


def _delete_state_cookie(response: Response) -> None:
    response.delete_cookie(
        key=StateCookieSerializer.COOKIE_NAME,
        path="/",
    )


def _json_error(
    status_code: int, detail: str, *, delete_state_cookie: bool = False
) -> JSONResponse:
    r = JSONResponse(status_code=status_code, content={"detail": detail})
    if delete_state_cookie:
        _delete_state_cookie(r)
    return r


# ---------------------------------------------------------------------------
# GET /auth/login
# ---------------------------------------------------------------------------

@router.get("/auth/login", include_in_schema=False)
async def login(request: Request, next: str | None = None):
    _require_oidc_mode()
    client: OIDCClient = get_oidc_client()

    state, nonce = OIDCClient.generate_state_and_nonce()
    code_verifier, code_challenge = OIDCClient.generate_pkce_pair()

    try:
        auth_url = await client.build_authorization_url(
            state=state, nonce=nonce, code_challenge=code_challenge
        )
    except Exception as e:
        logger.error(f"Failed to build OIDC authorization URL: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OIDC discovery failed — see server logs.",
        ) from e

    payload = StateCookiePayload(
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        next_url=_sanitize_next_url(next),
    )
    cookie_value = _state_serializer().dumps(payload)

    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key=StateCookieSerializer.COOKIE_NAME,
        value=cookie_value,
        max_age=StateCookieSerializer.DEFAULT_TTL_SECONDS,
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
async def callback(request: Request, code: str | None = None, state: str | None = None):
    _require_oidc_mode()

    if not code or not state:
        return _json_error(
            status.HTTP_400_BAD_REQUEST,
            "Missing 'code' or 'state' query parameter.",
            delete_state_cookie=True,
        )

    # --- 1. Parse state cookie -------------------------------------------------
    cookie_raw = request.cookies.get(StateCookieSerializer.COOKIE_NAME)
    if not cookie_raw:
        return _json_error(
            status.HTTP_400_BAD_REQUEST,
            "OIDC state cookie missing.",
            delete_state_cookie=True,
        )

    try:
        payload = _state_serializer().loads(cookie_raw)
    except ValueError as e:
        logger.warning(f"Invalid OIDC state cookie: {e}")
        return _json_error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid or expired OIDC state cookie.",
            delete_state_cookie=True,
        )

    # --- 2. CSRF check --------------------------------------------------------
    if state != payload.state:
        logger.warning("OIDC state mismatch between query and cookie")
        return _json_error(
            status.HTTP_400_BAD_REQUEST,
            "OIDC state mismatch.",
            delete_state_cookie=True,
        )

    # --- 3. Exchange code ------------------------------------------------------
    client: OIDCClient = get_oidc_client()
    try:
        bundle = await client.exchange_code(
            code=code,
            code_verifier=payload.code_verifier,
            expected_nonce=payload.nonce,
        )
    except Exception as e:
        logger.warning(f"OIDC code exchange failed: {e}")
        return _json_error(
            status.HTTP_400_BAD_REQUEST,
            f"OIDC code exchange failed: {e}",
            delete_state_cookie=True,
        )

    # --- 4. Extract claims -----------------------------------------------------
    sub = bundle.claims.get("sub")
    if not sub:
        return _json_error(
            status.HTTP_400_BAD_REQUEST,
            "ID token missing 'sub' claim.",
            delete_state_cookie=True,
        )

    email: str | None
    if _email_source() == "id_token":
        email = bundle.claims.get("email")
    else:
        # userinfo
        try:
            userinfo = await client.fetch_userinfo(bundle.access_token)
        except Exception as e:
            logger.warning(f"OIDC userinfo fetch failed: {e}")
            return _json_error(
                status.HTTP_400_BAD_REQUEST,
                "Failed to fetch userinfo from IdP.",
                delete_state_cookie=True,
            )
        email = userinfo.get("email")

    if not email:
        return _json_error(
            status.HTTP_400_BAD_REQUEST,
            "IdP did not return an email address.",
            delete_state_cookie=True,
        )

    email = email.strip().lower()

    # --- 5. Optional email-domain whitelist ------------------------------------
    allowed = _allowed_email_domains()
    if allowed:
        try:
            domain = email.split("@", 1)[1].lower()
        except IndexError:
            return _json_error(
                status.HTTP_400_BAD_REQUEST,
                "Invalid email address format.",
                delete_state_cookie=True,
            )
        if domain not in allowed:
            logger.warning(
                f"OIDC login rejected — email domain {domain!r} not in whitelist"
            )
            return _json_error(
                status.HTTP_403_FORBIDDEN,
                f"Email domain {domain!r} is not allowed.",
                delete_state_cookie=True,
            )

    # --- 6. User matching ------------------------------------------------------
    vdb = get_vectordb()

    user: dict[str, Any] | None = await vdb.get_user_by_external_id.remote(sub)
    if user is None:
        user = await vdb.get_user_by_email.remote(email)
        if user is None:
            logger.warning(
                f"OIDC login rejected — user not registered (email={email!r}, sub={sub!r})"
            )
            return _json_error(
                status.HTTP_403_FORBIDDEN,
                "User not registered",
                delete_state_cookie=True,
            )

        stored_ext = user.get("external_user_id")
        if stored_ext is None:
            # Backfill external_user_id = sub.
            try:
                await vdb.set_user_external_id.remote(user["id"], sub)
                logger.info(
                    f"Backfilled external_user_id for user_id={user['id']} (sub={sub!r})"
                )
            except Exception as e:
                logger.warning(
                    f"set_user_external_id failed for user_id={user['id']}: {e}"
                )
            # Re-fetch via sub — this proves the backfill actually won the race.
            user = await vdb.get_user_by_external_id.remote(sub)
            if user is None:
                return _json_error(
                    status.HTTP_403_FORBIDDEN,
                    "External user ID mismatch",
                    delete_state_cookie=True,
                )
        elif stored_ext != sub:
            logger.warning(
                "OIDC login rejected — external_user_id mismatch: "
                f"user_id={user['id']}, stored={stored_ext!r}, claim_sub={sub!r}"
            )
            return _json_error(
                status.HTTP_403_FORBIDDEN,
                "External user ID mismatch",
                delete_state_cookie=True,
            )

    # Defensive sanity check (should be impossible after the lookup-by-sub path).
    assert user.get("external_user_id") in (None, sub), (
        f"OIDC invariant violated: external_user_id={user.get('external_user_id')!r} "
        f"but matching sub={sub!r}"
    )

    # --- 7. Timestamps ---------------------------------------------------------
    now = _utcnow()
    expires_in = max(int(bundle.expires_in or 0), 60)
    access_token_expires_at = now + timedelta(seconds=expires_in)
    if bundle.refresh_token:
        session_expires_at = now + timedelta(days=7)
    else:
        session_expires_at = access_token_expires_at

    # --- 8. Issue session & encrypt ------------------------------------------
    plain, _hashed = issue_session_token()
    key = _token_encryption_key()
    id_token_encrypted = encrypt_token(bundle.id_token, key=key)
    access_token_encrypted = encrypt_token(bundle.access_token, key=key)
    refresh_token_encrypted = encrypt_token(bundle.refresh_token, key=key)
    sid = bundle.claims.get("sid")

    await vdb.create_oidc_session.remote(
        user_id=user["id"],
        sub=sub,
        sid=sid,
        session_token_plain=plain,
        id_token_encrypted=id_token_encrypted,
        access_token_encrypted=access_token_encrypted,
        refresh_token_encrypted=refresh_token_encrypted,
        access_token_expires_at=access_token_expires_at,
        session_expires_at=session_expires_at,
    )

    # --- 9. Build redirect: clear state cookie, set session cookie -----------
    next_url = _sanitize_next_url(payload.next_url)
    redirect = RedirectResponse(url=next_url, status_code=302)
    _delete_state_cookie(redirect)

    max_age = max(int((session_expires_at - now).total_seconds()), 1)
    redirect.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=plain,
        max_age=max_age,
        httponly=True,
        secure=_is_request_secure(request),
        samesite="lax",
        path="/",
    )

    logger.info(
        f"OIDC login success — user_id={user['id']}, sid={sid!r}, next={next_url!r}"
    )
    return redirect


# ---------------------------------------------------------------------------
# POST /auth/backchannel-logout
# ---------------------------------------------------------------------------

@router.post("/auth/backchannel-logout", include_in_schema=False)
async def backchannel_logout(logout_token: str = Form(...)):
    """IdP-initiated logout per OIDC Back-Channel Logout spec.

    Content-Type: ``application/x-www-form-urlencoded`` with field ``logout_token``.
    """
    _require_oidc_mode()

    client: OIDCClient = get_oidc_client()

    try:
        claims = await client.verify_logout_token(logout_token)
    except ValueError as e:
        logger.warning(f"Invalid back-channel logout token: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_request", "error_description": str(e)},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        logger.warning(f"Back-channel logout token verification failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_request"},
            headers={"Cache-Control": "no-store"},
        )

    if claims.sid:
        vdb = get_vectordb()
        count = await vdb.revoke_oidc_sessions_by_sid.remote(claims.sid)
        logger.info(
            f"Back-channel logout revoked sessions — sid={claims.sid!r}, count={count}"
        )
    else:
        # Plan §2 #10 limits back-channel logout scope to sid only.
        # Still return 200 to keep the IdP happy.
        logger.warning(
            f"Received sid-less back-channel logout token — not supported; "
            f"ignoring per implementation policy (sub={claims.sub!r})"
        )

    return Response(
        status_code=status.HTTP_200_OK,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# GET /auth/logout
# ---------------------------------------------------------------------------

@router.get("/auth/logout", include_in_schema=False)
async def logout(request: Request):
    _require_oidc_mode()

    vdb = get_vectordb()
    client: OIDCClient = get_oidc_client()

    # Look up & revoke the session; keep the id_token to forward as id_token_hint.
    id_token_hint: str | None = None
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value:
        session = await vdb.get_oidc_session_by_token.remote(cookie_value)
        if session:
            enc = session.get("id_token_encrypted")
            if enc:
                try:
                    id_token_hint = decrypt_token(enc, key=_token_encryption_key())
                except ValueError as e:
                    logger.warning(f"Failed to decrypt id_token for logout: {e}")
            try:
                await vdb.revoke_oidc_session_by_id.remote(session["id"])
            except Exception as e:
                logger.warning(f"Failed to revoke oidc_session during logout: {e}")

    # Build redirect target: IdP end_session if discovery provides one, else local.
    local_target = _post_logout_redirect_uri()
    redirect_target = local_target
    try:
        meta = await client.discover()
        end_session = meta.get("end_session_endpoint")
        if end_session:
            params = {
                "client_id": _oidc_client_id(),
                "post_logout_redirect_uri": local_target,
            }
            if id_token_hint:
                params["id_token_hint"] = id_token_hint
            redirect_target = f"{end_session}?{urlencode(params)}"
    except Exception as e:
        logger.warning(f"OIDC discovery failed during logout, redirecting locally: {e}")

    response = RedirectResponse(url=redirect_target, status_code=302)
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
            # naive datetime → iso str
            session_expires_at = exp.isoformat()
        except AttributeError:
            session_expires_at = str(exp)

    return {
        "user_id": user.get("id"),
        "email": user.get("email"),
        "auth_method": "oidc" if oidc_session else "token",
        "session_expires_at": session_expires_at,
    }
