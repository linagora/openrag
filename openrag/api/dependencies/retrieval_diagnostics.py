"""Authorization and resource limits for opt-in retrieval diagnostics."""

from __future__ import annotations

import os
import time
from functools import lru_cache

from fastapi import HTTPException, status
from limits import parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter


class RetrievalDiagnosticsGuard:
    """Keep expensive, sensitive retrieval diagnostics admin-only and bounded."""

    def __init__(self, rate_limit: str | None = None) -> None:
        self._limit = parse(rate_limit or os.getenv("RETRIEVAL_DIAGNOSTICS_RATE_LIMIT", "120/minute"))
        self._limiter = MovingWindowRateLimiter(MemoryStorage())

    async def authorize(self, user: object) -> None:
        if not isinstance(user, dict) or not user.get("is_admin", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Retrieval diagnostics require administrator privileges",
            )

        identity = f"user:{user.get('id', 'unknown')}"
        allowed = await self._limiter.hit(self._limit, "retrieval_diagnostics", identity)
        if allowed:
            return

        window = await self._limiter.get_window_stats(self._limit, "retrieval_diagnostics", identity)
        retry_after = max(1, int(window.reset_time - time.time()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Retrieval diagnostics rate limit exceeded. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )


@lru_cache(maxsize=1)
def get_retrieval_diagnostics_guard() -> RetrievalDiagnosticsGuard:
    return RetrievalDiagnosticsGuard()


__all__ = ["RetrievalDiagnosticsGuard", "get_retrieval_diagnostics_guard"]
