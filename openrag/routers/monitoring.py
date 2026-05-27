"""Prometheus ``/metrics`` endpoint for OpenRAG.

The middleware that powered this used to live alongside the endpoint
here; Phase 10C moved it to :mod:`api.middleware.instrumentation`
because middleware is request infrastructure, not a router concern. The
``/metrics`` endpoint itself still lives in this module — 10F moves it
to ``api/routers/admin/monitoring.py``. Until then ``MonitoringMiddleware``
is re-exported below so the legacy ``openrag/main.py`` import keeps
working through the strangler-fig window.
"""

import asyncio

from api.middleware.instrumentation import InstrumentationMiddleware
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from routers.utils import require_admin
from utils.monitoring import get_metrics

router = APIRouter()


# Legacy alias for ``openrag/main.py``; Phase 12 cleanup removes it once
# the last importer flips to ``api.middleware.instrumentation``.
MonitoringMiddleware = InstrumentationMiddleware


@router.get("/metrics", summary="Prometheus metrics endpoint", dependencies=[Depends(require_admin)])
async def prometheus_metrics():
    """Return all metrics in Prometheus text exposition format."""
    content = await asyncio.to_thread(get_metrics)
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
