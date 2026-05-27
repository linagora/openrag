"""Prometheus-compatible monitoring endpoints and middleware for OpenRAG."""

import asyncio
import time

from api.dependencies.auth import require_admin
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from utils.monitoring import get_metrics, record_request

router = APIRouter()

# -- Paths to exclude from metric recording (avoid self-referential noise) ---
_EXCLUDED_PREFIXES = ("/metrics", "/health_check", "/docs", "/openapi.json", "/redoc")


# -- Middleware --------------------------------------------------------------


class MonitoringMiddleware(BaseHTTPMiddleware):
    """Records request duration and status for every API call into Prometheus metrics."""

    async def dispatch(self, request: Request, call_next):
        """Time the request and record metrics, wrapping streaming bodies for accurate duration."""
        raw_path = request.url.path
        if any(raw_path.startswith(p) for p in _EXCLUDED_PREFIXES):
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - start
            route = self._get_route_template(request)
            record_request(request.method, route, 500, duration)
            raise

        # For streaming responses, wrap the body iterator so duration
        # covers the full transfer, not just time-to-first-byte.
        original_body_iterator = response.body_iterator
        route = self._get_route_template(request)
        status_code = response.status_code

        async def timed_body_iterator():
            metric_status_code = status_code
            try:
                async for chunk in original_body_iterator:
                    yield chunk
            except Exception:
                metric_status_code = 500
                raise
            finally:
                duration = time.perf_counter() - start
                record_request(request.method, route, metric_status_code, duration)

        response.body_iterator = timed_body_iterator()
        return response

    @staticmethod
    def _get_route_template(request: Request) -> str:
        """Return the FastAPI route template, or a fixed fallback to prevent label cardinality explosion."""
        route = request.scope.get("route")
        if route and hasattr(route, "path"):
            return route.path
        return "/-unresolved-"


# -- Endpoints ---------------------------------------------------------------


@router.get("/metrics", summary="Prometheus metrics endpoint", dependencies=[Depends(require_admin)])
async def prometheus_metrics():
    """Return all metrics in Prometheus text exposition format."""
    content = await asyncio.to_thread(get_metrics)
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
