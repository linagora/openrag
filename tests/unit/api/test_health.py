from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from api.routers.user.health import router
from fastapi import FastAPI


@pytest.mark.parametrize("initialized", [False, True])
async def test_liveness_does_not_depend_on_container(async_client_factory, initialized):
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(is_initialized=initialized)
    async with async_client_factory(app) as client:
        response = await client.get("/health_check")
    assert response.status_code == 200
    assert response.json() == "RAG API is up."


@pytest.mark.parametrize("container", [None, SimpleNamespace(is_initialized=False)])
async def test_readiness_rejects_incomplete_startup(async_client_factory, container):
    app = FastAPI()
    app.include_router(router)
    app.state.container = container
    async with async_client_factory(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


@pytest.mark.parametrize("dependency_status,expected_status", [("ok", 200), ("unavailable", 503), ("timeout", 503)])
async def test_readiness_reports_dependency_status(async_client_factory, dependency_status, expected_status):
    checks = {"postgres": "ok", "milvus": dependency_status}
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(
        is_initialized=True, readiness_service=SimpleNamespace(check=AsyncMock(return_value=checks))
    )
    async with async_client_factory(app) as client:
        response = await client.get("/ready")
    assert response.status_code == expected_status
    assert response.json() == {"status": "ready" if expected_status == 200 else "not_ready", "checks": checks}


async def test_model_failure_is_reported_without_gating_core_readiness(async_client_factory):
    checks = {"postgres": "ok", "milvus": "ok", "ray": "ok", "llm": "unavailable"}
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(
        is_initialized=True, readiness_service=SimpleNamespace(check=AsyncMock(return_value=checks))
    )
    async with async_client_factory(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": checks}
