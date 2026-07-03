"""Per-identity request rate limiting, tiered by path.

Keyed on the authenticated user, or the client IP for unauthenticated paths.
Runs after AuthMiddleware (registered before it in ``api.main`` so it executes
after auth populates ``request.state.user``). Limits are per-worker; use a Redis
storage to share them across workers.

Admin users bypass rate limiting entirely, mirroring the file-quota bypass in
``api.dependencies.auth`` — trusted operators (admin UI polling, bulk scripts)
should not be throttled.

Env: RATE_LIMIT_ENABLED (true), RATE_LIMIT_DEFAULT (600/minute),
RATE_LIMIT_AUTH (60/minute, /auth/*), RATE_LIMIT_CHAT (120/minute, /v1/*).
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


def _env_flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply per-identity moving-window rate limits, tiered by path prefix."""

    def __init__(self, app):
        super().__init__(app)
        self.enabled = _env_flag("RATE_LIMIT_ENABLED", True)
        self._limiter = MovingWindowRateLimiter(MemoryStorage())
        self._default = parse(os.environ.get("RATE_LIMIT_DEFAULT", "600/minute"))
        self._auth = parse(os.environ.get("RATE_LIMIT_AUTH", "60/minute"))
        self._chat = parse(os.environ.get("RATE_LIMIT_CHAT", "120/minute"))
        if self.enabled:
            logger.info(
                "Rate limiting enabled",
                default=str(self._default),
                auth=str(self._auth),
                chat=str(self._chat),
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

        # Admins bypass rate limiting entirely, mirroring the file-quota bypass
        # in api.dependencies.auth. Unauthenticated paths (/auth/*) have no user
        # on request.state, so this only ever exempts an authenticated admin.
        if self._is_admin(request):
            return await call_next(request)

        path = request.url.path
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
