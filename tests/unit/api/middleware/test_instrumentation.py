"""Smoke tests for :class:`InstrumentationMiddleware`.

Focus is the contract that matters for the stack: the middleware
records a metric for normal routes and skips the noisy self-referential
paths (``/metrics``, ``/health_check``, docs).
"""

from __future__ import annotations

import pytest
from api.middleware.instrumentation import InstrumentationMiddleware
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware


def test_records_metric_for_normal_route(monkeypatch) -> None:
    """A request to a regular route triggers ``record_request`` with the
    matched FastAPI route template, the response status, and a non-zero
    duration."""
    recorded: list[tuple[str, str, int, float]] = []

    def fake_record(method: str, path: str, status: int, duration: float) -> None:
        recorded.append((method, path, status, duration))

    # Patch the symbol the middleware imported at module load.
    monkeypatch.setattr("api.middleware.instrumentation.record_request", fake_record)

    app = FastAPI()
    app.add_middleware(InstrumentationMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    TestClient(app).get("/items/42")

    assert len(recorded) == 1
    method, route, status, duration = recorded[0]
    assert method == "GET"
    # Recorded as the route template, not the concrete URL — otherwise
    # Prometheus label cardinality explodes.
    assert route == "/items/{item_id}"
    assert status == 200
    assert duration >= 0


def test_skips_excluded_paths(monkeypatch) -> None:
    """``/metrics`` / ``/health_check`` / docs paths are excluded so the
    monitoring path itself does not inflate the request counter."""
    recorded: list[tuple[str, str, int, float]] = []

    monkeypatch.setattr(
        "api.middleware.instrumentation.record_request",
        lambda m, p, s, d: recorded.append((m, p, s, d)),
    )

    app = FastAPI()
    app.add_middleware(InstrumentationMiddleware)

    @app.get("/metrics")
    async def metrics() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/health_check")
    async def health() -> dict[str, str]:
        return {"ok": "1"}

    client = TestClient(app)
    client.get("/metrics")
    client.get("/health_check")
    client.get("/docs")
    client.get("/openapi.json")

    assert recorded == []


# ---------------------------------------------------------------------------
# Endpoint label resolution for requests that never set ``scope["route"]``.
#
# Only FastAPI's APIRoute/APIWebSocketRoute set that key. Everything else —
# Starlette Mounts onto raw ASGI apps, StaticFiles, and every request a
# middleware rejects before routing — used to collapse into one ``/-unresolved-``
# bucket, which is what made the dashboard's failing-route panel useless.
# ---------------------------------------------------------------------------


@pytest.fixture()
def labels(monkeypatch) -> list[tuple[str, str, int]]:
    """Collect ``(method, endpoint, status)`` for every recorded request."""
    recorded: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        "api.middleware.instrumentation.record_request",
        lambda m, p, s, d: recorded.append((m, p, s)),
    )
    return recorded


def _instrumented_app() -> FastAPI:
    """An app shaped like the real one: a submounted Chainlit-style sub-app
    with its own Socket.IO mount, root ``/assets`` statics, and a route behind
    a middleware that rejects before routing."""

    async def socketio_app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sub = FastAPI()
    sub.mount("/ws/socket.io", socketio_app)

    @sub.get("/user")
    async def sub_user() -> dict[str, str]:
        return {"user": "anon"}

    app = FastAPI()
    app.mount("/chainlit", sub)
    app.mount("/assets", socketio_app)

    @app.get("/users/info")
    async def users_info() -> dict[str, str]:  # never reached: rejected below
        return {"id": "1"}

    @app.get("/partition/")
    async def partitions() -> dict[str, str]:
        return {"ok": "1"}

    class RejectingMiddleware(BaseHTTPMiddleware):
        """Stands in for AuthMiddleware: answers before routing ever runs."""

        async def dispatch(self, request, call_next):
            if request.url.path == "/users/info":
                return JSONResponse({"detail": "Missing token"}, status_code=403)
            return await call_next(request)

    app.add_middleware(RejectingMiddleware)
    app.add_middleware(InstrumentationMiddleware)
    return app


def test_socket_io_polling_is_labelled_by_its_mount(labels) -> None:
    """Chainlit's Socket.IO transport is a Mount onto a raw ASGI app, so
    routing never sets ``scope["route"]``. It is named after the mount it was
    served by — the label chat traffic used to pile up under."""
    TestClient(_instrumented_app()).get("/chainlit/ws/socket.io/?transport=polling")

    assert labels == [("GET", "/chainlit/ws/socket.io", 200)]


def test_static_mount_is_labelled_by_its_mount(labels) -> None:
    """A root-level static mount reports the mount, not a per-file path —
    otherwise every asset filename would mint its own Prometheus series."""
    TestClient(_instrumented_app()).get("/assets/pdf.worker.mjs")

    assert labels == [("GET", "/assets", 200)]


def test_submounted_route_keeps_its_mount_prefix(labels) -> None:
    """A route inside a submounted app carries a path relative to that app
    (``/user``). Recorded raw it would merge with the parent app's own routes,
    so the mount prefix is restored."""
    TestClient(_instrumented_app()).get("/chainlit/user")

    assert labels == [("GET", "/chainlit/user", 200)]


def test_rejected_before_routing_is_labelled_with_its_route(labels) -> None:
    """An auth rejection short-circuits above the router, so nothing sets
    ``scope["route"]`` — but the route exists, and the failing-route panel is
    only useful if the rejection is attributed to it."""
    TestClient(_instrumented_app()).get("/users/info")

    assert labels == [("GET", "/users/info", 403)]


def test_unknown_path_is_bounded(labels) -> None:
    """A URL no route can serve gets one fixed label. This is the cardinality
    guard: a scanner walking random URLs must not create a series per URL."""
    client = TestClient(_instrumented_app())
    client.get("/no-such-path-xyz")
    client.get("/another-nonexistent-path")

    assert labels == [
        ("GET", "/-not-found-", 404),
        ("GET", "/-not-found-", 404),
    ]


def test_slash_redirect_is_attributed_to_its_route(labels) -> None:
    """``redirect_slashes`` answers ``/partition`` with a 307 without matching
    any route; the redirect belongs to the route it points at."""
    TestClient(_instrumented_app(), follow_redirects=False).get("/partition")

    assert labels == [("GET", "/partition/", 307)]
