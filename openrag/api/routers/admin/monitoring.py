"""Prometheus ``/metrics`` endpoint for OpenRAG.

The instrumentation middleware that records the metrics this endpoint
serves lives in :mod:`api.middleware.instrumentation`.
"""

import asyncio

from api.dependencies.auth import require_admin
from core.observability.monitoring import get_metrics
from fastapi import APIRouter, Depends
from fastapi.responses import Response

router = APIRouter()


@router.get("/metrics", summary="Prometheus metrics endpoint", dependencies=[Depends(require_admin)])
async def prometheus_metrics():
    """Return all metrics in Prometheus text exposition format."""
    content = await asyncio.to_thread(get_metrics)
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
