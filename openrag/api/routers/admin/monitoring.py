"""Prometheus ``/metrics`` endpoint for OpenRAG.

The instrumentation middleware that records the metrics this endpoint
serves lives in :mod:`api.middleware.instrumentation`.
"""

import asyncio

from api.dependencies.auth import require_admin
from core.observability.monitoring import (
    clear_ingest_task_counts,
    get_metrics,
    set_ingest_task_counts,
)
from core.utils.logging import get_logger
from di.providers import get_job_service
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

logger = get_logger()

router = APIRouter()

#: Bound on the actor round-trip taken during a scrape. Prometheus' own scrape
#: timeout is typically 10s and covers the whole response, so this leaves room
#: for the rest of the exposition; a TaskStateManager that cannot answer in two
#: seconds is itself the outage, and waiting longer would only turn a missing
#: gauge into a failed scrape of everything else.
_QUEUE_INFO_TIMEOUT_SECONDS = 2.0


async def _refresh_ingest_tasks(request: Request) -> None:
    """Sample the in-flight task counts for ``openrag_ingest_tasks``.

    Resolved here rather than as a route dependency on purpose. ``get_job_service``
    raises 503 when the container is absent, and a degraded boot is exactly when
    the remaining metrics are worth having — making it a dependency would take
    the whole endpoint down with the backlog gauge.

    Any failure withdraws the gauge and serves everything else. A scrape that
    returns HTTP metrics without the backlog is a small gap; a scrape that fails
    returns nothing at all.
    """
    try:
        service = get_job_service(request)
        counts = await asyncio.wait_for(service.get_active_task_counts(), timeout=_QUEUE_INFO_TIMEOUT_SECONDS)
        set_ingest_task_counts(counts)
    except Exception as exc:  # noqa: BLE001 - the scrape must survive any of this
        clear_ingest_task_counts()
        logger.debug(f"ingest task gauge not sampled for this scrape: {exc}")


@router.get("/metrics", summary="Prometheus metrics endpoint", dependencies=[Depends(require_admin)])
async def prometheus_metrics(request: Request):
    """Return all metrics in Prometheus text exposition format."""
    await _refresh_ingest_tasks(request)
    content = await asyncio.to_thread(get_metrics)
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
