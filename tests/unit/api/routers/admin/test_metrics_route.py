"""``GET /metrics`` — Prometheus exposition guarded by a dedicated token.

The route bypasses :class:`AuthMiddleware` (so no user token is ever needed to
scrape) and instead checks ``server.metrics_token`` /
``server.metrics_allow_unauthenticated``. It fails closed: with neither set,
every scrape gets ``403``. A token means the scraper must present exactly that
bearer; the explicit opt-in opens the endpoint to anyone reaching the API port.
Admin/user tokens are never accepted here — one mechanism, no fallback.
"""

from __future__ import annotations

import pytest
from api.routers.admin.monitoring import get_metrics_access, router
from core.config.infrastructure import ServerConfig
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio


def _app(token: str | None = None, *, allow_unauthenticated: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    server = ServerConfig(metrics_token=token, metrics_allow_unauthenticated=allow_unauthenticated)
    app.dependency_overrides[get_metrics_access] = lambda: server
    return app


async def test_metrics_is_closed_by_default(async_client_factory):
    """Neither METRICS_TOKEN nor METRICS_ALLOW_UNAUTHENTICATED: fail closed, and
    say why so the operator finds the fix in the 403 body."""
    async with async_client_factory(_app()) as client:
        response = await client.get("/metrics")

    assert response.status_code == 403
    assert "METRICS_TOKEN" in response.json()["detail"]
    assert "METRICS_ALLOW_UNAUTHENTICATED" in response.json()["detail"]


async def test_metrics_closed_by_default_ignores_admin_bearer(async_client_factory):
    async with async_client_factory(_app()) as client:
        response = await client.get("/metrics", headers={"Authorization": "Bearer admin-token"})

    assert response.status_code == 403


async def test_metrics_is_open_when_explicitly_allowed(async_client_factory):
    async with async_client_factory(_app(allow_unauthenticated=True)) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert b"openrag_http_requests_total" in response.content


async def test_metrics_open_accepts_empty_bearer(async_client_factory):
    """The bundled compose Prometheus always sends ``credentials_file``; with an
    empty file that is ``Authorization: Bearer `` — harmless on an open route."""
    async with async_client_factory(_app(allow_unauthenticated=True)) as client:
        response = await client.get("/metrics", headers={"Authorization": "Bearer "})

    assert response.status_code == 200


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


async def test_token_wins_over_allow_unauthenticated(async_client_factory):
    """Both set: the stricter setting applies — a configured token is enforced."""
    async with async_client_factory(_app("scrape-secret", allow_unauthenticated=True)) as client:
        anonymous = await client.get("/metrics")
        with_token = await client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})

    assert anonymous.status_code == 403
    assert with_token.status_code == 200
