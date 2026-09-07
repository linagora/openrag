"""``GET /metrics`` — Prometheus exposition guarded by an optional dedicated token.

The route bypasses :class:`AuthMiddleware` (so no user token is ever needed to
scrape) and instead checks ``server.metrics_token``: unset means the endpoint is
open, set means the scraper must present exactly that bearer. Admin/user tokens
are never accepted here — one mechanism, no fallback.
"""

from __future__ import annotations

import pytest
from api.routers.admin.monitoring import get_metrics_token, router
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio


def _app(token: str | None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_metrics_token] = lambda: token
    return app


async def test_metrics_is_open_when_no_token_configured(async_client_factory):
    async with async_client_factory(_app(None)) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert b"openrag_http_requests_total" in response.content


async def test_metrics_rejects_missing_bearer_when_token_configured(async_client_factory):
    async with async_client_factory(_app("scrape-secret")) as client:
        response = await client.get("/metrics")

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid metrics token"}


async def test_metrics_rejects_wrong_bearer_when_token_configured(async_client_factory):
    async with async_client_factory(_app("scrape-secret")) as client:
        response = await client.get("/metrics", headers={"Authorization": "Bearer admin-token"})

    assert response.status_code == 403


async def test_metrics_accepts_matching_bearer(async_client_factory):
    async with async_client_factory(_app("scrape-secret")) as client:
        response = await client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})

    assert response.status_code == 200
    assert b"openrag_http_requests_total" in response.content


async def test_metrics_requires_bearer_scheme(async_client_factory):
    """A bare token (no ``Bearer`` prefix) is not a valid credential."""
    async with async_client_factory(_app("scrape-secret")) as client:
        response = await client.get("/metrics", headers={"Authorization": "scrape-secret"})

    assert response.status_code == 403
