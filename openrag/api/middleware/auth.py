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

import asyncio
import hashlib
import os
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal
from urllib.parse import quote

from core.auth.chainlit import CHAINLIT_IDP_LOGOUT_COOKIE_NAME, CHAINLIT_TOKEN_COOKIE_NAME
from core.config.auth import AuthBypassConfig
from core.utils.consts import DEFAULT_ADMIN_USER_ID
from core.utils.logging import get_logger
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
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


def _login_redirect(request: Request, path: str) -> RedirectResponse:
    # The query comes from the scope for the same reason ``path`` does (see
    # ``AuthMiddleware.dispatch``).
    query = request.scope.get("query_string", b"").decode()
    next_path = f"{path}?{query}" if query else path
    return RedirectResponse(url=f"/auth/login?next={quote(next_path, safe='')}", status_code=302)


def _idp_logout_redirect() -> RedirectResponse:
    """Finish a Chat logout at the IdP, as the admin UI's logout does.

    Chat logout revokes the OpenRAG session and sets the cookie that leads
    here on the page reload that follows. ``/auth/logout`` revokes the session
    again if Chat logout couldn't, redirects to the IdP, and clears that
    cookie: until it has, every Chat page load comes back here.
    """
    return RedirectResponse(url="/auth/logout", status_code=302)


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


_RECENT_AUTHENTICATED_TOKEN_CACHE_MAX_ENTRIES = 1024
_CredentialKind = Literal["session", "token"]
_Credential = tuple[str, _CredentialKind]
_Authenticated = tuple[Any, Any]
_GENERATED_USER_TOKEN_PATTERN = re.compile(r"or-[0-9a-f]{32}")


class AuthFailureRateLimiter:
    """Track failed authentication attempts and pre-check their client IP bucket."""

    def __init__(self) -> None:
        self.enabled = _env_flag("RATE_LIMIT_ENABLED", True)
        self._limiter = MovingWindowRateLimiter(MemoryStorage())
        self._limit = (
            parse(os.environ.get("RATE_LIMIT_AUTH_FAILURE", os.environ.get("RATE_LIMIT_AUTH", "20/minute")))
            if self.enabled
            else None
        )

    @property
    def window_seconds(self) -> int:
        """Return the configured failure window for successful-token cache expiry."""
        return self._limit.get_expiry() if self._limit is not None else 60

    @staticmethod
    def _identity(request: Request) -> str:
        client = request.client
        return f"ip:{client.host}" if client else "ip:unknown"

    @staticmethod
    def _safe_log_value(value: str, *, max_len: int = 200) -> str:
        return value.replace("\r", "\\r").replace("\n", "\\n").replace("\x00", "\\0")[:max_len]

    async def is_limited(self, request: Request) -> bool:
        if not self.enabled or self._limit is None:
            return False
        identity = self._identity(request)
        tier = "auth-failure"
        return not await self._limiter.test(self._limit, tier, identity)

    async def limited_response(self, request: Request) -> JSONResponse:
        identity = self._identity(request)
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
            path=self._safe_log_value(str(request.scope["path"])),
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
        self._recently_authenticated_tokens: OrderedDict[tuple[str, bytes, _CredentialKind], float] = OrderedDict()
        self._recently_rejected_sessions: OrderedDict[tuple[str, bytes], float] = OrderedDict()
        self._credential_locks: dict[tuple[str, bytes], tuple[asyncio.Lock, int]] = {}
        # A bucket can fill near the end of one window; retain the last
        # successful token through the remaining lockout window too.
        self._recently_authenticated_token_ttl = 2 * self._auth_failure_limiter.window_seconds

    async def _auth_failure(self, request: Request, *, status_code: int, detail: str) -> JSONResponse:
        limited = await self._auth_failure_limiter.record_failure(request)
        if limited is not None:
            return limited
        return JSONResponse(status_code=status_code, content={"detail": detail})

    @staticmethod
    def _request_token(request: Request, path: str) -> str | None:
        if path.startswith("/static"):
            return getattr(request.state, "original_token", None)
        auth = request.headers.get("authorization", "")
        if auth and auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1]
        return None

    @staticmethod
    def _token_fingerprint(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def _has_recently_authenticated_token(self, identity: str, token: str, kind: _CredentialKind) -> bool:
        cache_key = (identity, self._token_fingerprint(token), kind)
        now = time.monotonic()
        expires_at = self._recently_authenticated_tokens.get(cache_key)
        if expires_at is None:
            return False
        if expires_at <= now:
            self._recently_authenticated_tokens.pop(cache_key, None)
            return False
        return True

    def _remember_authenticated_token(self, identity: str, token: str, kind: _CredentialKind) -> None:
        if kind == "token" and _GENERATED_USER_TOKEN_PATTERN.fullmatch(token) is None:
            return

        cache_key = (identity, self._token_fingerprint(token), kind)
        now = time.monotonic()
        cache = self._recently_authenticated_tokens

        # Every entry has the same TTL. Only successful validation changes
        # its position, so the oldest entries are also the first to expire.
        while cache and next(iter(cache.values())) <= now:
            cache.popitem(last=False)

        cache[cache_key] = now + self._recently_authenticated_token_ttl
        cache.move_to_end(cache_key)
        while len(cache) > _RECENT_AUTHENTICATED_TOKEN_CACHE_MAX_ENTRIES:
            cache.popitem(last=False)

    def _forget_authenticated_token(self, identity: str, token: str, kind: _CredentialKind) -> None:
        self._recently_authenticated_tokens.pop((identity, self._token_fingerprint(token), kind), None)

    def _has_recently_rejected_session(self, identity: str, token: str) -> bool:
        cache_key = (identity, self._token_fingerprint(token))
        expires_at = self._recently_rejected_sessions.get(cache_key)
        if expires_at is None:
            return False
        if expires_at <= time.monotonic():
            self._recently_rejected_sessions.pop(cache_key, None)
            return False
        return True

    def _remember_rejected_session(self, identity: str, token: str) -> None:
        cache_key = (identity, self._token_fingerprint(token))
        now = time.monotonic()
        cache = self._recently_rejected_sessions

        while cache and next(iter(cache.values())) <= now:
            cache.popitem(last=False)

        cache[cache_key] = now + self._recently_authenticated_token_ttl
        cache.move_to_end(cache_key)
        while len(cache) > _RECENT_AUTHENTICATED_TOKEN_CACHE_MAX_ENTRIES:
            cache.popitem(last=False)

    def _forget_rejected_session(self, identity: str, token: str) -> None:
        self._recently_rejected_sessions.pop((identity, self._token_fingerprint(token)), None)

    def _lock_recently_authenticated_tokens(
        self, identity: str, credentials: tuple[str, ...]
    ) -> AbstractAsyncContextManager[None]:
        keys = sorted({(identity, self._token_fingerprint(token)) for token in credentials})
        return self._lock_credentials(keys)

    @asynccontextmanager
    async def _lock_credentials(self, keys: list[tuple[str, bytes]]) -> AsyncIterator[None]:
        entries: list[tuple[tuple[str, bytes], asyncio.Lock]] = []
        acquired: list[asyncio.Lock] = []
        try:
            for key in keys:
                current = self._credential_locks.get(key)
                lock, references = current if current is not None else (asyncio.Lock(), 0)
                self._credential_locks[key] = (lock, references + 1)
                entries.append((key, lock))

            for _, lock in entries:
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
            for key, lock in entries:
                current = self._credential_locks.get(key)
                if current is None or current[0] is not lock:
                    continue
                if current[1] == 1:
                    del self._credential_locks[key]
                else:
                    self._credential_locks[key] = (lock, current[1] - 1)

    def _resolve_auth_service(self, request: Request) -> tuple[Any, JSONResponse | None]:
        """Resolve the auth service, or the response to send when it is unavailable.

        The resolver goes through `di.providers.get_container`, which raises
        `HTTPException(503)` on a degraded boot. Middleware runs outside the
        router, so FastAPI's exception handlers never see this — uncaught it
        becomes a 500 `[UNEXPECTED_ERROR]`, the opposite of the "serving degraded
        (503)" the boot guard logged (#937). Every call site that is not already
        inside its own ``except`` must go through here.

        The resolver's own status, detail and headers are relayed rather than
        flattened to 503, so a future resolver reporting something else — or
        attaching ``Retry-After`` — is not masked.
        """
        # The routed path is percent-decoded, so `%0A` arrives as a newline and
        # would forge a line in the text log format.
        path = AuthFailureRateLimiter._safe_log_value(request.scope["path"])
        try:
            return self._get_auth_service(request), None
        except HTTPException as exc:
            logger.warning("Auth service unavailable", reason="service_unavailable", status=exc.status_code, path=path)
            return None, JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail or "Service unavailable"},
                headers=exc.headers,
            )
        except RuntimeError:
            logger.warning("Auth service unavailable", reason="service_unavailable", status=503, path=path)
            return None, JSONResponse(status_code=503, content={"detail": "Service unavailable"})

    async def _authenticate_oidc_session(
        self,
        auth_service: Any,
        identity: str,
        token: str,
        enc_key: str,
        *,
        limited: bool,
        remember_rejection: bool,
    ) -> _Authenticated | None:
        if limited and not self._has_recently_authenticated_token(identity, token, "session"):
            return None
        session = await auth_service.get_oidc_session_by_token_for_request(token)
        needs_refresh = session is not None and auth_service.oidc_session_needs_refresh(session)
        if session is not None and not needs_refresh:
            if limited and not self._has_recently_authenticated_token(identity, token, "session"):
                return None
            user = await auth_service.get_user_for_request(session["user_id"])
            # A session with no refresh token can expire while the user lookup runs.
            if not session.get("refresh_token_encrypted"):
                needs_refresh = auth_service.oidc_session_needs_refresh(session)
            if user is not None and not needs_refresh:
                if limited and not self._has_recently_authenticated_token(identity, token, "session"):
                    return None
                self._forget_rejected_session(identity, token)
                self._remember_authenticated_token(identity, token, "session")
                return user, session

        if session is not None and needs_refresh:
            observed_session = session
            revocation_failed = False
            # Rotation is per session, independent of IP or transport. Read
            # again under the lock so we act on the latest stored tokens.
            # The lock is held through the user lookup and the cache renewal:
            # a same-identity waiter whose cached entry expired while it
            # waited must see the renewed entry, not a 429 after the refresh
            # succeeded.
            async with self._lock_credentials([("oidc-session", self._token_fingerprint(token))]):
                if limited and not self._has_recently_authenticated_token(identity, token, "session"):
                    return None
                session = await auth_service.get_oidc_session_by_token_for_request(token)
                if session is not None:
                    if not auth_service.oidc_session_was_refreshed_since(observed_session, session):
                        session_id = session["id"]
                        session = await auth_service.refresh_session_if_needed(session=session, enc_key=enc_key)
                        if session is None:
                            try:
                                await auth_service.revoke_oidc_session_by_id_for_request(session_id)
                            except Exception as exc:
                                revocation_failed = True
                                logger.bind(error=str(exc)).warning("Failed to revoke invalid OIDC session")
                if session is None:
                    self._forget_authenticated_token(identity, token, "session")
                    if remember_rejection and not revocation_failed:
                        self._remember_rejected_session(identity, token)
                    if revocation_failed:
                        # The session may still be active; preserve its
                        # precedence rather than treating it as rejected.
                        return None
                    return None, None
                user = await auth_service.get_user_for_request(session["user_id"])
                if user is not None:
                    self._forget_rejected_session(identity, token)
                    self._remember_authenticated_token(identity, token, "session")
                    return user, session
                self._forget_authenticated_token(identity, token, "session")
                if remember_rejection:
                    self._remember_rejected_session(identity, token)
                return None, None

        self._forget_authenticated_token(identity, token, "session")
        if remember_rejection:
            self._remember_rejected_session(identity, token)
        return None, None

    async def _validate_credentials(
        self,
        auth_service: Any,
        credentials: tuple[_Credential, ...],
        *,
        enc_key: str,
        identity: str,
        cookie_token: str | None,
        eligible: set[_Credential] | None = None,
    ) -> tuple[_Authenticated | None, tuple[_Credential, ...], _Credential | None]:
        skipped: list[_Credential] = []
        for credential in credentials:
            token, kind = credential
            if eligible is not None and (
                credential not in eligible or not self._has_recently_authenticated_token(identity, token, kind)
            ):
                skipped.append(credential)
                continue

            if kind == "session":
                result = await self._authenticate_oidc_session(
                    auth_service,
                    identity,
                    token,
                    enc_key,
                    limited=eligible is not None,
                    remember_rejection=kind == "session" and bool(cookie_token) and token == cookie_token,
                )
                if result is None:
                    skipped.append(credential)
                    continue
                user, session = result
            else:
                session = None
                user = await auth_service.get_user_by_token_for_request(token)
                if user is not None:
                    self._remember_authenticated_token(identity, token, kind)
                else:
                    self._forget_authenticated_token(identity, token, kind)

            if user is not None:
                return (user, session), tuple(skipped), credential
        return None, tuple(skipped), None

    async def _authenticate_request(
        self,
        request: Request,
        *,
        path: str,
        auth_mode: str,
        oidc_gated: bool,
        enc_key: str,
        token: str | None,
        cookie_token: str | None,
    ) -> tuple[Any, Any, Any] | Response:
        limiter = self._auth_failure_limiter
        identity = limiter._identity(request)
        candidates: tuple[tuple[str | None, _CredentialKind], ...] = (
            (cookie_token, "session"),
            (token if auth_mode == "oidc" else None, "session"),
            (token, "token"),
        )
        credentials = tuple(
            dict.fromkeys(
                (value, kind) for value, kind in candidates if value is not None and (value or kind == "token")
            )
        )
        limited = await limiter.is_limited(request)
        if limited:
            recent = tuple(
                credential
                for credential in credentials
                if self._has_recently_authenticated_token(identity, *credential)
            )
            if not recent:
                return await limiter.limited_response(request)

            # Serialize limited revalidation so waiters observe cache eviction
            # before repeating a failed lookup.
            # Token checks share a client-scoped cache entry, so serialize them
            # to avoid duplicate validation after eviction. OIDC sessions use
            # their own lock only when refresh is needed; fresh session reads
            # must remain concurrent under lockout.
            recent_tokens = tuple(value for value, kind in recent if kind == "token")
            async with self._lock_recently_authenticated_tokens(identity, recent_tokens):
                limited = await limiter.is_limited(request)
                if limited:
                    eligible = {
                        credential
                        for credential in recent
                        if self._has_recently_authenticated_token(identity, *credential)
                    }
                    if not eligible:
                        return await limiter.limited_response(request)
                    first_eligible_index = next(
                        index for index, credential in enumerate(credentials) if credential in eligible
                    )
                    eligible_values = {value for value, _kind in eligible}
                    blocked_by_earlier_credential = False
                    for value, kind in credentials[:first_eligible_index]:
                        if value in eligible_values:
                            continue
                        if (
                            kind == "session"
                            and bool(cookie_token)
                            and value == cookie_token
                            and self._has_recently_rejected_session(identity, value)
                        ):
                            # A rejected cookie has no identity precedence to
                            # preserve. Renew it while skipped so a long
                            # lockout does not eventually block a valid token.
                            self._remember_rejected_session(identity, value)
                            continue
                        blocked_by_earlier_credential = True
                        break
                    if blocked_by_earlier_credential:
                        limited = await limiter.is_limited(request)
                        # Do not let a lower-priority credential replace an
                        # earlier, distinct credential that cannot be checked
                        # during lockout. If the bucket reopened, normal auth
                        # below can safely check credentials in their usual order.
                        if limited:
                            return await limiter.limited_response(request)
                    if limited:
                        auth_service, unavailable = self._resolve_auth_service(request)
                        if unavailable is not None:
                            return unavailable
                        result, skipped, selected = await self._validate_credentials(
                            auth_service,
                            credentials,
                            enc_key=enc_key,
                            identity=identity,
                            cookie_token=cookie_token,
                            eligible=eligible,
                        )

        if not limited:
            auth_service, unavailable = self._resolve_auth_service(request)
            if unavailable is not None:
                return unavailable
            result, _, _ = await self._validate_credentials(
                auth_service, credentials, enc_key=enc_key, identity=identity, cookie_token=cookie_token
            )
        elif skipped or result is None:
            if await limiter.is_limited(request):
                if result is None:
                    return await limiter.limited_response(request)
                blocked_by_skipped_credential = False
                if selected is not None:
                    for skipped_token, kind in skipped:
                        if skipped_token == selected[0]:
                            continue
                        if (
                            kind == "session"
                            and bool(cookie_token)
                            and skipped_token == cookie_token
                            and self._has_recently_rejected_session(identity, skipped_token)
                        ):
                            self._remember_rejected_session(identity, skipped_token)
                            continue
                        blocked_by_skipped_credential = True
                        break
                if blocked_by_skipped_credential:
                    return await limiter.limited_response(request)
            elif skipped:
                # Earlier skipped candidates can take precedence after the
                # bucket reopens. Keep a successful result if none validate.
                retry, _, _ = await self._validate_credentials(
                    auth_service, skipped, enc_key=enc_key, identity=identity, cookie_token=cookie_token
                )
                if retry is not None:
                    result = retry

        if result is not None:
            user, session = result
            return user, session, auth_service
        if auth_mode == "token":
            # The API's existing token-mode contract is a 403 with this detail.
            detail = "Invalid token" if token is not None else "Missing token"
            return await self._auth_failure(request, status_code=403, detail=detail)
        if oidc_gated or (auth_mode == "oidc" and is_ui_path(path, bypass_config=self._bypass_config)):
            return _login_redirect(request, path)
        return await self._auth_failure(request, status_code=401, detail="Unauthenticated")

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
            # Resolved before the bypass list, so on a degraded boot an unguarded
            # call here would 500 every path, /health_check and /ready included.
            auth_service, unavailable = self._resolve_auth_service(request)
            if unavailable is not None:
                return unavailable
            user = await auth_service.get_user_for_request(DEFAULT_ADMIN_USER_ID)
            user_partitions = await auth_service.list_user_partitions_for_request(DEFAULT_ADMIN_USER_ID)
            request.state.user = user
            request.state.user_partitions = user_partitions
            request.state.oidc_session = None
            return await call_next(request)

        # --- Bypass list (docs, health, /auth/* callbacks, chainlit).
        # Decisions below key on ``scope["path"]``, the path the router
        # dispatches on. ``request.url.path`` is parsed back out of a URL rebuilt
        # from the Host header, so it is not guaranteed to be the same string.
        path = request.scope["path"]

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
                if request.cookies.get(CHAINLIT_IDP_LOGOUT_COOKIE_NAME):
                    return _idp_logout_redirect()
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
                    return _login_redirect(request, path)
            return await call_next(request)

        result = await self._authenticate_request(
            request,
            path=path,
            auth_mode=auth_mode,
            oidc_gated=oidc_gated,
            enc_key=enc_key,
            token=self._request_token(request, path),
            cookie_token=request.cookies.get(SESSION_COOKIE_NAME) if auth_mode == "oidc" else None,
        )

        if isinstance(result, Response):
            return result
        user, session, auth_service = result
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
