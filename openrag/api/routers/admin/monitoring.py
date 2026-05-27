"""Prometheus ``/metrics`` endpoint for OpenRAG (Phase 10F move).

The middleware that powered this used to live alongside the endpoint in
``routers/monitoring.py``; Phase 10C moved it to
:mod:`api.middleware.instrumentation`. Phase 10F now moves the
``/metrics`` endpoint into this module so both halves live under the
``api/`` namespace. The legacy ``routers/monitoring.py`` becomes a
re-export shim that also keeps the ``MonitoringMiddleware`` alias alive
for callers still on the old import path.
"""

import asyncio

from api.dependencies.auth import require_admin
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from utils.monitoring import get_metrics

router = APIRouter()


@router.get("/metrics", summary="Prometheus metrics endpoint", dependencies=[Depends(require_admin)])
async def prometheus_metrics():
    """Return all metrics in Prometheus text exposition format."""
    content = await asyncio.to_thread(get_metrics)
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
