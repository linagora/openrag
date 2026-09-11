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
from starlette.routing import Match, Mount

# Paths to exclude from metric recording — avoids self-referential noise
# and inflated counters on probe traffic.
_EXCLUDED_PREFIXES = ("/metrics", "/health_check", "/ready", "/docs", "/openapi.json", "/redoc")

# Label for a URL no route in the app can serve. A *fixed* string is the whole
# point: the endpoint label must stay bounded, or a scanner walking 10k random
# URLs mints 10k Prometheus time series.
_NOT_FOUND = "/-not-found-"

# Depth limit for the mount walk below — cheap insurance against a pathological
# or self-referential mount tree.
_MAX_MOUNT_DEPTH = 4


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

        # ``call_next`` runs the rest of the stack against this very scope dict,
        # so routing's mutations are visible once it returns — and so are the
        # rewrites a submounted app makes on its way in. Snapshot what the
        # request looked like on entry: ``root_path``, because a ``Mount``
        # appends its own prefix to it and that delta is what turns a sub-app's
        # route path back into the browser-facing one; and ``app``, because a
        # mounted Starlette app overwrites ``scope["app"]`` with itself, so
        # afterwards the scope no longer points at the routing table the
        # request actually entered through.
        entry = (request.scope.get("root_path", ""), request.scope.get("app"))

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - start
            route = self._get_route_template(request, entry)
            record_request(request.method, route, 500, duration)
            raise

        # Wrap streaming bodies so duration covers the full transfer,
        # not just time-to-first-byte.
        original_body_iterator = response.body_iterator
        route = self._get_route_template(request, entry)
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

    @classmethod
    def _get_route_template(cls, request: Request, entry: tuple[str, object] = ("", None)) -> str:
        """Return the browser-facing route template for this request.

        Three cases, in order:

        1. **Routing matched a FastAPI route.** ``scope["route"]`` holds it.
           Its ``path`` is relative to the app that owns it, so a route inside
           a submounted app is prefixed with the mount path routing appended to
           ``root_path`` — otherwise Chainlit's ``/chainlit/user`` would be
           recorded as ``/user`` and silently merge with the parent app's own
           routes.
        2. **Nothing set** ``scope["route"]``. Only FastAPI's ``APIRoute`` /
           ``APIWebSocketRoute`` set that key; Starlette's ``Route``, ``Mount``
           and ``WebSocketRoute`` never do. So this covers ``Mount``s onto raw
           ASGI apps (Chainlit's Socket.IO transport), ``StaticFiles``, *and*
           every request a middleware rejected **before** routing ran (auth
           403s, rate-limit 429s). Walking the routing table by hand recovers
           the template the request was aimed at — which is what makes an auth
           rejection show up under the endpoint it was actually for.
        3. **Nothing matches**: a real 404, recorded as ``/-not-found-``.
        """
        entry_root_path, entry_app = entry
        scope = request.scope
        path = getattr(scope.get("route"), "path", None)
        if isinstance(path, str):
            mount_prefix = scope.get("root_path", "")[len(entry_root_path) :]
            return f"{mount_prefix}{path}"
        return cls._match_route_table(request, entry_root_path, entry_app) or _NOT_FOUND

    @classmethod
    def _match_route_table(cls, request: Request, entry_root_path: str, entry_app: object) -> str | None:
        """Best-effort re-run of routing for a request that never reached it."""
        routes = getattr(entry_app or request.scope.get("app"), "routes", None)
        if not routes:
            return None

        # Routing may already have rewritten ``root_path`` on the way in (a
        # Mount that matched, then 404'd inside it). Rewind it so the walk
        # starts where the request did.
        scope = dict(request.scope)
        scope["root_path"] = entry_root_path

        label = cls._walk(routes, scope, "", 0)
        if label is not None:
            return label

        # ``redirect_slashes``: Starlette answers ``/partition`` with a 307 to
        # ``/partition/`` without ever matching a route. Try the other spelling
        # so the redirect is attributed to the route it points at.
        path = scope.get("path", "")
        alt = path.rstrip("/") if path.endswith("/") else f"{path}/"
        if alt and alt != path:
            scope["path"] = alt
            return cls._walk(routes, scope, "", 0)
        return None

    @classmethod
    def _walk(cls, routes, scope: dict, prefix: str, depth: int) -> str | None:
        """Return the first matching route's template, prefixed by its mounts."""
        partial: str | None = None
        for route in routes:
            try:
                match, child_scope = route.matches(scope)
            except Exception:
                # A third-party route type with a stricter scope contract than
                # the synthetic one we hand it. Never let metrics break a request.
                continue
            if match is Match.NONE:
                continue
            path = getattr(route, "path", None)
            if not isinstance(path, str):
                continue
            if isinstance(route, Mount):
                # ``Mount`` never reports PARTIAL, so this is a full match.
                # Descend to name the sub-route (``…/ws/socket.io``) instead of
                # stopping at the mount point, but fall back to the mount itself
                # for a raw ASGI app that exposes no routing table of its own.
                if depth >= _MAX_MOUNT_DEPTH:
                    return f"{prefix}{path}"
                sub_scope = {**scope, **child_scope}
                deeper = cls._walk(getattr(route, "routes", None) or [], sub_scope, f"{prefix}{path}", depth + 1)
                return deeper or f"{prefix}{path}"
            if match is Match.FULL:
                return f"{prefix}{path}"
            if partial is None:
                # PARTIAL means the path matched but the method did not (405).
                # Still the right template to attribute the request to.
                partial = f"{prefix}{path}"
        return partial


__all__ = ["InstrumentationMiddleware"]
