"""RequestTimeoutMiddleware — pass-through stub (Phase 10C placeholder).

Reserves the middleware slot called out in the Phase 10C plan. The
full implementation (per-request ``asyncio.timeout`` enforcement with
exemptions for streaming responses and large uploads) is a
post-refactor feature; until then the dispatch is a no-op so the stack
order stays stable across the cutover.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """No-op placeholder. See module docstring for the future contract."""

    async def dispatch(self, request: Request, call_next):
        return await call_next(request)


__all__ = ["RequestTimeoutMiddleware"]
