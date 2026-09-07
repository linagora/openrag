"""Prometheus ``/metrics`` endpoint for OpenRAG.

The instrumentation middleware that records the metrics this endpoint
serves lives in :mod:`api.middleware.instrumentation`.

Access model — one mechanism, no fallback: the path is bypassed by
:class:`api.middleware.auth.AuthMiddleware` (a scraper never holds a user
token) and the route checks ``server.metrics_token`` (``METRICS_TOKEN``)
itself. Unset → open to anyone who can reach the API port, the usual
posture on an internal network. Set → the scraper must send
``Authorization: Bearer <METRICS_TOKEN>``; admin/user tokens are refused.
"""

import asyncio
import secrets

from core.config import load_config
from core.observability.monitoring import get_metrics
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

router = APIRouter()


def get_metrics_token() -> str | None:
    """Resolve the configured scrape token.

    Reads the process-level config rather than the request container so the
    endpoint keeps answering while the container is degraded (a scrape must
    not turn into a 503 just because Milvus is down — that is exactly when
    the metrics are wanted). Overridable via ``app.dependency_overrides``.
    """
    return load_config().server.metrics_token


def require_metrics_token(request: Request, expected: str | None = Depends(get_metrics_token)) -> None:
    """Reject the request unless it carries the configured metrics bearer."""
    if expected is None:
        return
    scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(credential.strip(), expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid metrics token")


@router.get("/metrics", summary="Prometheus metrics endpoint", dependencies=[Depends(require_metrics_token)])
async def prometheus_metrics():
    """Return all metrics in Prometheus text exposition format."""
    content = await asyncio.to_thread(get_metrics)
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
