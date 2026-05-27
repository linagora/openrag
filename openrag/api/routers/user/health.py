"""Health / version endpoints (Phase 10F move).

Lifts the inline ``/health_check`` and ``/version`` routes out of
``openrag/api/main.py`` per the Phase 10A plan ("Inline health endpoint
(3 lines) -> Dedicated api/routers/user/health.py"). Both routes are
unauthenticated (``AuthMiddleware`` bypasses them via the bypass list)
so they can be hit by load balancers, kubernetes probes, and ``curl``
without a token.

The actual ``app.version`` value is set in ``api/main.py`` and read off
``request.app.version`` at call time so this router does not need its
own access to ``importlib.metadata``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health_check", summary="Health check endpoint for API", dependencies=[])
async def health_check(request: Request) -> str:
    # TODO: Error reporting about llm and vlm.
    return "RAG API is up."


@router.get("/version", summary="Get openRAG version", dependencies=[])
def get_version(request: Request) -> dict[str, str]:
    return {"version": request.app.version}
