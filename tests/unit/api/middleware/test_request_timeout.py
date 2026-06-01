"""Sanity check for the Phase 10C :class:`RequestTimeoutMiddleware` stub.

The class is intentionally a pass-through until the full
``asyncio.timeout`` implementation lands post-refactor; this test just
guards that the slot stays a no-op and the import path is stable so the
api/main stack registration does not accidentally start dropping
requests.
"""

from __future__ import annotations

from api.middleware.request_timeout import RequestTimeoutMiddleware
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_stub_passes_request_through_unchanged() -> None:
    app = FastAPI()
    app.add_middleware(RequestTimeoutMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "1"}

    response = TestClient(app).get("/ping")
    assert response.status_code == 200
    assert response.json() == {"ok": "1"}
