"""Per-identity request rate limiting, tiered by path.

Keyed on the authenticated user, or the client IP for unauthenticated paths.
Runs after AuthMiddleware (registered before it in ``api.main`` so it executes
after auth populates ``request.state.user``). Limits are per-worker; use a Redis
storage to share them across workers.

Admin users bypass rate limiting entirely, mirroring the file-quota bypass in
``api.dependencies.auth`` — trusted operators (admin UI polling, bulk scripts)
should not be throttled.

The Chainlit subtree is exempt: ``AuthMiddleware`` bypasses it (Chainlit runs
its own header-auth callback), so no user ever lands on ``request.state`` and
every request there keys on the client IP — which behind the admin-ui reverse
proxy is a single container address shared by every chat user. A per-identity
limit that cannot tell identities apart throttles the whole deployment at once,
and Chainlit's Socket.IO transport at ``/chainlit/ws/socket.io`` issues one
HTTP request per emitted packet whenever it falls back to long-polling.

Env: RATE_LIMIT_ENABLED (true), RATE_LIMIT_DEFAULT (600/minute),
RATE_LIMIT_AUTH (60/minute, /auth/*), RATE_LIMIT_CHAT (120/minute, /v1/*),
RATE_LIMIT_EXEMPT_PATHS (/chainlit/,/assets/ — CSV of path prefixes). Any
configured prefix that would cover /auth/ is dropped (with a warning) so the
brute-force surface can never be exempted, even by operator misconfiguration.
"""

import os
import time

from core.utils.logging import get_logger
from limits import parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = get_logger()

# Path prefixes the limiter never touches, matched with ``str.startswith`` — so the
# trailing slash is load-bearing. These are the auth-bypassed subtrees from
# ``api.middleware.auth.is_bypass_path``: unauthenticated by design, so a request
# there carries no user and can only ever be keyed by the proxy's IP. The prefixes
# end in ``/`` so a sibling like ``/chainlithack`` is NOT swept into the ``/chainlit``
# exemption — it stays rate-limited. Bare ``/chainlit`` (the 307 to ``/chainlit/``) is
# therefore metered at the default tier, but that is one request per page load, so it
# never bites. ``/auth/*`` is deliberately NOT here: it is the brute-force surface and
# IP keying is the point.
DEFAULT_EXEMPT_PREFIXES: tuple[str, ...] = ("/chainlit/", "/assets/")

# The one prefix RATE_LIMIT_EXEMPT_PATHS is never allowed to cover, even via a
# misconfigured operator override (e.g. "/auth/" or an overly broad "/"): it is
# the brute-force surface and must always stay rate-limited.
_PROTECTED_PREFIX = "/auth/"


def _env_flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_prefixes(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """CSV of path prefixes. Unset uses ``default``; set-but-empty disables exemptions."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply per-identity moving-window rate limits, tiered by path prefix."""

    def __init__(self, app):
        super().__init__(app)
        self.enabled = _env_flag("RATE_LIMIT_ENABLED", True)
        self._limiter = MovingWindowRateLimiter(MemoryStorage())
        exempt = _env_prefixes("RATE_LIMIT_EXEMPT_PATHS", DEFAULT_EXEMPT_PREFIXES)
        # A prefix that is itself a prefix of "/auth/" (e.g. "/auth/", "/a", "/")
        # would exempt the brute-force surface via path.startswith() below — drop
        # those regardless of where they came from, so /auth/ can never be opted
        # out of rate limiting by RATE_LIMIT_EXEMPT_PATHS.
        unsafe = tuple(p for p in exempt if _PROTECTED_PREFIX.startswith(p))
        if unsafe:
            logger.warning(
                "Ignoring RATE_LIMIT_EXEMPT_PATHS entries that would exempt /auth/",
                prefixes=",".join(unsafe),
            )
        self._exempt = tuple(p for p in exempt if p not in unsafe)
        # Only parse the limit configs when rate limiting is enabled: a malformed
        # RATE_LIMIT_* value must not crash boot when the feature is turned off
        # (``dispatch`` short-circuits before touching these when disabled).
        self._default = self._auth = self._chat = None
        if self.enabled:
            self._default = parse(os.environ.get("RATE_LIMIT_DEFAULT", "600/minute"))
            self._auth = parse(os.environ.get("RATE_LIMIT_AUTH", "60/minute"))
            self._chat = parse(os.environ.get("RATE_LIMIT_CHAT", "120/minute"))
            logger.info(
                "Rate limiting enabled",
                default=str(self._default),
                auth=str(self._auth),
                chat=str(self._chat),
                exempt=",".join(self._exempt) or "(none)",
            )

    def _limit_for(self, path: str):
        if path.startswith("/auth/"):
            return self._auth, "auth"
        if path.startswith("/v1/"):
            return self._chat, "chat"
        return self._default, "default"

    @staticmethod
    def _identity(request: Request) -> str:
        # user is a dict set by AuthMiddleware; fall back to client IP.
        user = getattr(request.state, "user", None)
        user_id = user.get("id") if isinstance(user, dict) else None
        if user_id is not None:
            return f"user:{user_id}"
        client = request.client
        return f"ip:{client.host}" if client else "ip:unknown"

    @staticmethod
    def _is_admin(request: Request) -> bool:
        # AuthMiddleware sets request.state.user to a dict carrying "is_admin".
        user = getattr(request.state, "user", None)
        return bool(user.get("is_admin")) if isinstance(user, dict) else False

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)

        path = request.url.path

        # Exempt prefixes are checked before the admin bypass on purpose: these
        # paths never run through AuthMiddleware, so request.state.user is unset
        # and _is_admin() is False even for an admin's browser session.
        if self._exempt and path.startswith(self._exempt):
            return await call_next(request)

        # Admins bypass rate limiting entirely, mirroring the file-quota bypass
        # in api.dependencies.auth. Unauthenticated paths (/auth/*) have no user
        # on request.state, so this only ever exempts an authenticated admin.
        if self._is_admin(request):
            return await call_next(request)

        limit, tier = self._limit_for(path)
        identity = self._identity(request)

        # Key by tier so each tier has its own budget.
        allowed = await self._limiter.hit(limit, tier, identity)
        if not allowed:
            stats = await self._limiter.get_window_stats(limit, tier, identity)
            retry_after = max(1, int(stats.reset_time - time.time()))
            logger.warning("Rate limit exceeded", path=path, tier=tier, identity=identity)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please retry later.", "extra": {}},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
