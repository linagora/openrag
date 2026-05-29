"""InstrumentationMiddleware — Prometheus request metrics.

The middleware lives here because it is request infrastructure (every
request flows through it); the ``/metrics`` endpoint it powers lives in
:mod:`api.routers.admin.monitoring`.
"""

from __future__ import annotations

import time

from core.observability.monitoring import record_request
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

# Paths to exclude from metric recording — avoids self-referential noise
# and inflated counters on probe traffic.
_EXCLUDED_PREFIXES = ("/metrics", "/health_check", "/docs", "/openapi.json", "/redoc")


class InstrumentationMiddleware(BaseHTTPMiddleware):
    """Record request duration and status for every API call into Prometheus.

    Streaming responses are wrapped so the recorded duration covers the
    full transfer rather than just time-to-first-byte; if the iterator
    raises mid-stream the metric is still emitted with a 500 status.
    """

    async def dispatch(self, request: Request, call_next):
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

        # Wrap streaming bodies so duration covers the full transfer,
        # not just time-to-first-byte.
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
        """Return the FastAPI route template, or a fixed fallback so
        unmatched URLs don't explode Prometheus label cardinality."""
        route = request.scope.get("route")
        if route and hasattr(route, "path"):
            return route.path
        return "/-unresolved-"


__all__ = ["InstrumentationMiddleware"]
