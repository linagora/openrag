from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from api.dependencies.auth import require_admin
from api.routers.admin.monitoring import router
from fastapi import FastAPI


@pytest.mark.parametrize("refresh_error", [None, RuntimeError("database unavailable")])
async def test_metrics_remains_available_while_refreshing_readiness(async_client_factory, monkeypatch, refresh_error):
    refresh = AsyncMock(side_effect=refresh_error)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: {"id": "admin", "is_admin": True}
    app.state.container = SimpleNamespace(
        is_initialized=True,
        readiness_service=SimpleNamespace(snapshot=refresh),
    )
    monkeypatch.setattr("api.routers.admin.monitoring.get_metrics", lambda: b"metric 1\n")

    async with async_client_factory(app) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.content == b"metric 1\n"
    refresh.assert_awaited_once()
