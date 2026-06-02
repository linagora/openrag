from __future__ import annotations

from typing import Any

import pytest
from api.dependencies.auth import require_admin
from api.routers.admin import model_endpoints, presets
from di.providers import get_model_endpoint_service, get_preset_service
from fastapi import FastAPI


def _model_endpoint_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "name": "default",
        "model_type": "llm",
        "endpoint": "http://llm:8000/v1",
        "model_name": "mistral",
        "batch_size": 32,
        "timeout": 30.0,
        "extra": {},
        "is_default": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _preset_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "name": "default",
        "preset_type": "retrieval",
        "config": {"type": "single", "top_k": 50},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class FakeModelEndpointService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create_model_endpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create", payload))
        return _model_endpoint_row(**payload)

    async def list_model_endpoints(self, model_type: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list", {"model_type": model_type}))
        return [_model_endpoint_row(model_type=model_type or "llm")]

    async def get_model_endpoint(self, name: str, model_type: str) -> dict[str, Any]:
        self.calls.append(("get", {"name": name, "model_type": model_type}))
        return _model_endpoint_row(name=name, model_type=model_type)

    async def update_model_endpoint(self, name: str, model_type: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update", {"name": name, "model_type": model_type, **fields}))
        return _model_endpoint_row(**{"name": name, "model_type": model_type, **fields})

    async def delete_model_endpoint(self, name: str, model_type: str) -> None:
        self.calls.append(("delete", {"name": name, "model_type": model_type}))

    async def set_default(self, model_type: str, name: str) -> dict[str, Any]:
        self.calls.append(("set_default", {"name": name, "model_type": model_type}))
        return _model_endpoint_row(name=name, model_type=model_type, is_default=True)

    async def validate_endpoint(self, name: str, model_type: str) -> dict[str, Any]:
        self.calls.append(("validate", {"name": name, "model_type": model_type}))
        return {"reachable": True, "model_found": True, "models_served": ["mistral"], "detail": None}


class FakePresetService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create_preset(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create", payload))
        return _preset_row(**payload)

    async def list_presets(self, preset_type: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list", {"preset_type": preset_type}))
        return [_preset_row(preset_type=preset_type or "retrieval")]

    async def get_preset(self, name: str, preset_type: str) -> dict[str, Any]:
        self.calls.append(("get", {"name": name, "preset_type": preset_type}))
        return _preset_row(name=name, preset_type=preset_type)

    async def update_preset(self, name: str, preset_type: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update", {"name": name, "preset_type": preset_type, **fields}))
        return _preset_row(**{"name": name, "preset_type": preset_type, **fields})

    async def delete_preset(self, name: str, preset_type: str) -> None:
        self.calls.append(("delete", {"name": name, "preset_type": preset_type}))


def _build_app(
    *,
    model_service: FakeModelEndpointService | None = None,
    preset_service: FakePresetService | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(model_endpoints.router, prefix="/model-endpoints")
    app.include_router(presets.router, prefix="/presets")
    app.dependency_overrides[require_admin] = lambda: {"id": "admin", "is_admin": True}
    if model_service is not None:
        app.dependency_overrides[get_model_endpoint_service] = lambda: model_service
    if preset_service is not None:
        app.dependency_overrides[get_preset_service] = lambda: preset_service
    return app


@pytest.mark.asyncio
async def test_create_model_endpoint_normalizes_payload(async_client_factory):
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/model-endpoints/",
            json={
                "name": " default ",
                "model_type": "llm",
                "endpoint": " http://llm:8000/v1/ ",
                "model_name": "mistral",
            },
        )

    assert response.status_code == 201
    assert model_service.calls == [
        (
            "create",
            {
                "name": "default",
                "model_type": "llm",
                "endpoint": "http://llm:8000/v1",
                "model_name": "mistral",
                "batch_size": 32,
                "timeout": 30.0,
                "extra": {},
                "is_default": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_update_model_endpoint_forwards_only_provided_fields(async_client_factory):
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.put(
            "/model-endpoints/llm/default",
            json={"endpoint": "http://llm-next:8000/v1", "timeout": 60},
        )

    assert response.status_code == 200
    assert model_service.calls == [
        (
            "update",
            {
                "name": "default",
                "model_type": "llm",
                "endpoint": "http://llm-next:8000/v1",
                "timeout": 60.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_validate_model_endpoint_uses_route_identity(async_client_factory):
    model_service = FakeModelEndpointService()
    app = _build_app(model_service=model_service)

    async with async_client_factory(app) as client:
        response = await client.post("/model-endpoints/llm/default/validate")

    assert response.status_code == 200
    assert response.json()["reachable"] is True
    assert model_service.calls == [("validate", {"name": "default", "model_type": "llm"})]


@pytest.mark.asyncio
async def test_preset_options_return_registered_choices(async_client_factory):
    app = _build_app()

    async with async_client_factory(app) as client:
        response = await client.get("/presets/options")

    assert response.status_code == 200
    body = response.json()
    assert body["chunking_strategies"] == ["recursive_splitter"]
    assert set(body["retrieval_types"]) == {"single", "multiQuery", "hyde"}
    assert body["reranker_providers"] == ["infinity", "openai"]


@pytest.mark.asyncio
async def test_create_preset_forwards_schema_payload(async_client_factory):
    preset_service = FakePresetService()
    app = _build_app(preset_service=preset_service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/presets/",
            json={"name": " default ", "preset_type": "retrieval", "config": {"type": "single"}},
        )

    assert response.status_code == 201
    assert preset_service.calls == [
        ("create", {"name": "default", "preset_type": "retrieval", "config": {"type": "single"}})
    ]


@pytest.mark.asyncio
async def test_update_preset_forwards_only_provided_fields(async_client_factory):
    preset_service = FakePresetService()
    app = _build_app(preset_service=preset_service)

    async with async_client_factory(app) as client:
        response = await client.put(
            "/presets/retrieval/default",
            json={"config": {"type": "hyde", "top_k": 20}},
        )

    assert response.status_code == 200
    assert preset_service.calls == [
        (
            "update",
            {
                "name": "default",
                "preset_type": "retrieval",
                "config": {"type": "hyde", "top_k": 20},
            },
        )
    ]
