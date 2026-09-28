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

The admin UI does not read ``/metrics``: it uses ``GET /monitoring/metrics``
(``admin_router`` below), the same exposition behind the ordinary admin gate.
"""

import asyncio
import secrets

from api.dependencies.auth import require_admin
from core.config import load_config
from core.config.infrastructure import ServerConfig
from core.observability.monitoring import (
    clear_ingest_task_counts,
    get_metrics,
    set_ingest_task_counts,
)
from core.utils.logging import get_logger
from di.providers import get_job_service
from fastapi import APIRouter, Depends, HTTPException, Request, status
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


#: Serialises refresh-then-collect. ``openrag_ingest_tasks`` is a process-global
#: gauge, so two concurrent scrapes can interleave: one clears or overwrites the
#: snapshot the other is about to serialise, and a response goes out describing
#: state that never existed. Prometheus scrapes on a timer, and a second scraper
#: — an agent, a curious operator, the admin UI polling — is enough to overlap.
#:
#: The lock covers the collect as well as the refresh, not just the write: the
#: race is between one request's sample and another's exposition, so guarding
#: only the sampling would leave it intact.
_scrape_lock = asyncio.Lock()

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


async def _render_metrics(request: Request) -> Response:
    # Both routes render through here, so the lock and the ingest sample belong
    # here rather than on one of them: the race is between any two scrapes,
    # whichever door they came in by.
    async with _scrape_lock:
        container = getattr(request.app.state, "container", None)
        if container is not None and container.is_initialized:
            try:
                await container.readiness_service.snapshot()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A scrape must remain available while readiness dependencies fail.
                pass
        await _refresh_ingest_tasks(request)
        content = await asyncio.to_thread(get_metrics)
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/metrics", summary="Prometheus metrics endpoint", dependencies=[Depends(require_metrics_token)])
async def prometheus_metrics(request: Request):
    """Return all metrics in Prometheus text exposition format."""
    return await _render_metrics(request)


# Same exposition for a signed-in admin (the admin UI's System > Metrics tab).
# Mounted under ``/monitoring`` (an API prefix, so unauthenticated calls get a
# JSON 401/403 rather than a login redirect) and gated by ``require_admin``
# like the other ops routes. Kept apart from ``/metrics`` on purpose: the
# scraper credential must never touch the Postgres token lookup, and an admin
# session must never need the scrape secret.
admin_router = APIRouter(dependencies=[Depends(require_admin)])


@admin_router.get("/metrics", summary="Prometheus metrics for a signed-in admin")
async def prometheus_metrics_for_admin(request: Request):
    """Return the same exposition as ``GET /metrics``, gated by the admin role."""
    return await _render_metrics(request)
