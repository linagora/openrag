"""Smoke tests for :class:`InstrumentationMiddleware`.

Focus is the contract that matters for the stack: the middleware
records a metric for normal routes and skips the noisy self-referential
paths (``/metrics``, ``/health_check``, docs).
"""

from __future__ import annotations

from api.middleware.instrumentation import InstrumentationMiddleware
from fastapi import FastAPI
from fastapi.testclient import TestClient


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
