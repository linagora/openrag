"""Prometheus ``/metrics`` endpoint for OpenRAG.

The instrumentation middleware that records the metrics this endpoint
serves lives in :mod:`api.middleware.instrumentation`.

Access model — one mechanism, no fallback: the path is bypassed by
:class:`api.middleware.auth.AuthMiddleware` (a scraper never holds a user
token) and the route checks ``server.metrics_token`` (``METRICS_TOKEN``)
itself. It fails closed:

* ``METRICS_TOKEN`` set → the scraper must send
  ``Authorization: Bearer <METRICS_TOKEN>``; admin/user tokens are refused.
* ``METRICS_ALLOW_UNAUTHENTICATED=true`` (and no token) → open to anyone who
  can reach the API port. A deliberate opt-in: the API port is what the
  Ingress / admin-ui proxy forwards, so "no token" must never be the default.
* neither → 403 on every scrape, with the two settings named in the body.
"""

import asyncio
import secrets

from core.config import load_config
from core.config.infrastructure import ServerConfig
from core.observability.monitoring import get_metrics
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

router = APIRouter()

_DISABLED_DETAIL = (
    "Metrics endpoint disabled: set METRICS_TOKEN, or METRICS_ALLOW_UNAUTHENTICATED=true to serve it without a token"
)


def get_metrics_access() -> ServerConfig:
    """Resolve the ``server`` config block that governs scrape access.

    Reads the process-level config rather than the request container so the
    endpoint keeps answering while the container is degraded (a scrape must
    not turn into a 503 just because Milvus is down — that is exactly when
    the metrics are wanted). Overridable via ``app.dependency_overrides``.
    """
    return load_config().server


def describe_metrics_access(server: ServerConfig) -> str | None:
    """Startup warning for the two non-default access states, else ``None``.

    Prometheus only surfaces "403 Forbidden" on its targets page, so the API
    log is where an operator learns why the bundled Grafana stays empty.
    """
    if server.metrics_token is not None:
        return None
    if server.metrics_allow_unauthenticated:
        return "GET /metrics is open to anyone reaching the API port (METRICS_ALLOW_UNAUTHENTICATED=true)"
    return "GET /metrics is disabled (403): set METRICS_TOKEN, or METRICS_ALLOW_UNAUTHENTICATED=true if the path is blocked at the edge"


def require_metrics_token(request: Request, server: ServerConfig = Depends(get_metrics_access)) -> None:
    """Reject the request unless it carries the configured metrics bearer."""
    expected = server.metrics_token
    if expected is None:
        if server.metrics_allow_unauthenticated:
            return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DISABLED_DETAIL)
    scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(credential.strip(), expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid metrics token")


@router.get("/metrics", summary="Prometheus metrics endpoint", dependencies=[Depends(require_metrics_token)])
async def prometheus_metrics():
    """Return all metrics in Prometheus text exposition format."""
    content = await asyncio.to_thread(get_metrics)
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
