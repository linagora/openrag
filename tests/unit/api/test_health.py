from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from api.routers.user.health import router
from core.config.model_endpoints import ModelEndpointConfig
from core.models.readiness import (
    ConfigurationReferenceReadiness,
    ModelEndpointDiscovery,
    ModelEndpointReadiness,
    ModelEndpointTarget,
    ReadinessSnapshot,
)
from fastapi import FastAPI
from services.orchestrators.readiness_service import ReadinessService


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
    assert response.json() == {
        "status": "not_ready",
        "checks": {"startup": "unavailable"},
        "model_endpoints": [],
        "configuration_references": [],
    }


@pytest.mark.parametrize("dependency_status,expected_status", [("ok", 200), ("unavailable", 503), ("timeout", 503)])
async def test_readiness_reports_dependency_status(async_client_factory, dependency_status, expected_status):
    checks = {"postgres": "ok", "milvus": dependency_status}
    snapshot = ReadinessSnapshot(checks=checks)
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(
        is_initialized=True, readiness_service=SimpleNamespace(snapshot=AsyncMock(return_value=snapshot))
    )
    async with async_client_factory(app) as client:
        response = await client.get("/ready")
    assert response.status_code == expected_status
    assert response.json() == {
        "status": "ready" if expected_status == 200 else "not_ready",
        "checks": checks,
        "model_endpoints": [],
        "configuration_references": [],
    }


async def test_model_failure_is_reported_without_gating_core_readiness(async_client_factory):
    checks = {"postgres": "ok", "milvus": "ok", "ray": "ok", "llm": "unavailable"}
    snapshot = ReadinessSnapshot(
        checks=checks,
        model_endpoints=(ModelEndpointReadiness(provider="large-context", kind="llm", status="unavailable"),),
        configuration_references=(ConfigurationReferenceReadiness(kind="indexation_preset", count=2),),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(
        is_initialized=True, readiness_service=SimpleNamespace(snapshot=AsyncMock(return_value=snapshot))
    )
    async with async_client_factory(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": checks,
        "model_endpoints": [{"provider": "configured", "kind": "llm", "status": "unavailable"}],
        "configuration_references": [{"kind": "indexation_preset", "count": 2, "status": "unresolvable"}],
    }


def _target(provider, kind, *, url="https://models.test/v1", model="served", is_default=False):
    return ModelEndpointTarget(
        provider=provider,
        kind=kind,
        config=ModelEndpointConfig(name=provider, endpoint=url, model_name=model),
        is_default=is_default,
    )


async def _ready_with_discovery(async_client_factory, discover):
    """/ready over a real ReadinessService with healthy core checks and the
    summary kinds production wires, so the embedder's status is derived the
    way it is in production rather than written into a snapshot by hand."""
    service = ReadinessService(
        {"postgres": AsyncMock(), "milvus": AsyncMock(), "ray": AsyncMock()},
        discover_model_endpoints=discover,
        summary_model_kinds=("embedder", "llm"),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(is_initialized=True, readiness_service=service)
    async with async_client_factory(app) as client:
        return await client.get("/ready")


@pytest.mark.parametrize(
    "embedder_url,embedder_targets,expected_embedder",
    [
        # The server is up but serves another model: the #1099 upgrade case.
        ("https://embedder.test/v1", True, "unavailable"),
        # The server answers with an error.
        ("https://broken.test/v1", True, "unavailable"),
        # No default embedder registered.
        (None, False, "unresolvable"),
    ],
)
async def test_an_unusable_default_embedder_gates_readiness(
    async_client_factory, respx_mock, embedder_url, embedder_targets, expected_embedder
):
    """Uploads and retrieval both need the default embedder (#1099): reporting
    ready without it kept broken pods in service."""
    respx_mock.get("https://models.test/v1/models").respond(200, json={"data": [{"id": "served"}]})
    respx_mock.get("https://embedder.test/v1/models").respond(200, json={"data": [{"id": "another-model"}]})
    respx_mock.get("https://broken.test/v1/models").respond(503)
    targets = [_target("chat", "llm", is_default=True)]
    if embedder_targets:
        targets.append(_target("embed", "embedder", url=embedder_url, model="indexed-model", is_default=True))
    discover = AsyncMock(return_value=ModelEndpointDiscovery(targets=tuple(targets)))

    response = await _ready_with_discovery(async_client_factory, discover)

    assert response.json()["checks"]["embedder"] == expected_embedder
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


async def test_a_served_default_embedder_does_not_gate_readiness(async_client_factory, respx_mock):
    """Control for the test above: the same wiring reports ready once the
    embedder serves its model, so the 503s there come from the embedder."""
    respx_mock.get("https://models.test/v1/models").respond(200, json={"data": [{"id": "served"}]})
    discover = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(_target("chat", "llm", is_default=True), _target("embed", "embedder", is_default=True))
        )
    )

    response = await _ready_with_discovery(async_client_factory, discover)

    assert response.json()["checks"]["embedder"] == "ok"
    assert response.status_code == 200


async def test_an_embedder_probe_timeout_does_not_gate_readiness(async_client_factory, respx_mock):
    """Model probes share one short deadline with discovery, so a timeout says
    the round was slow, not that the embedder is down; gating on it would pull
    every replica out of service at once."""
    respx_mock.get("https://models.test/v1/models").respond(200, json={"data": [{"id": "served"}]})
    respx_mock.get("https://slow.test/v1/models").mock(side_effect=httpx.ReadTimeout("timed out"))
    discover = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(
                _target("chat", "llm", is_default=True),
                _target("embed", "embedder", url="https://slow.test/v1", is_default=True),
            )
        )
    )

    response = await _ready_with_discovery(async_client_factory, discover)

    assert response.json()["checks"]["embedder"] == "timeout"
    assert response.status_code == 200


@pytest.mark.parametrize("failure", [RuntimeError("catalog query failed"), TimeoutError()])
async def test_a_model_discovery_failure_does_not_gate_on_the_embedder(async_client_factory, failure):
    """When discovery fails the embedder carries discovery's own status, which
    can read "unavailable" without anything having probed the embedder."""
    discover = AsyncMock(side_effect=failure)

    response = await _ready_with_discovery(async_client_factory, discover)

    assert response.json()["checks"]["model_endpoint_discovery"] != "ok"
    assert response.json()["checks"]["embedder"] == response.json()["checks"]["model_endpoint_discovery"]
    assert response.status_code == 200


async def test_a_non_default_embedder_outage_does_not_gate_readiness(async_client_factory, respx_mock):
    """A partition's own embedder is reported, like other per-partition
    endpoints, without taking the whole API out of service."""
    respx_mock.get("https://models.test/v1/models").respond(200, json={"data": [{"id": "served"}]})
    respx_mock.get("https://partition-embedder.test/v1/models").respond(503)
    discover = AsyncMock(
        return_value=ModelEndpointDiscovery(
            targets=(
                _target("chat", "llm", is_default=True),
                _target("embed", "embedder", is_default=True),
                _target("legal-embed", "embedder", url="https://partition-embedder.test/v1"),
            )
        )
    )

    response = await _ready_with_discovery(async_client_factory, discover)

    assert {"provider": "configured", "kind": "embedder", "status": "unavailable"} in response.json()["model_endpoints"]
    assert response.status_code == 200


async def test_readiness_without_model_discovery_does_not_gate_on_the_embedder(async_client_factory):
    service = ReadinessService({"postgres": AsyncMock(), "milvus": AsyncMock(), "ray": AsyncMock()})
    app = FastAPI()
    app.include_router(router)
    app.state.container = SimpleNamespace(is_initialized=True, readiness_service=service)
    async with async_client_factory(app) as client:
        response = await client.get("/ready")
    assert "embedder" not in response.json()["checks"]
    assert response.status_code == 200
