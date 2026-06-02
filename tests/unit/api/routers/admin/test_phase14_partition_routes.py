from __future__ import annotations

from typing import Any

import pytest
from api.dependencies.auth import require_partition_owner, require_partition_viewer
from api.routers.admin import partitions
from di.providers import get_partition_service
from fastapi import FastAPI


def _partition_detail(**overrides: Any) -> dict[str, Any]:
    row = {
        "name": "legal",
        "description": "Legal documents",
        "embedder": "default",
        "indexation_preset": "default",
        "retrieval_preset": "default",
        "indexation_pipeline": {"chunking": {"name": "recursive_splitter"}},
        "retrieval_pipeline": {"type": "single", "top_k": 50},
        "dimension": 1024,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class FakePartitionConfigService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def update_partition_config(self, partition: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update", {"partition": partition, **fields}))
        return _partition_detail(name=partition, **fields)

    async def get_partition_config(self, partition: str) -> dict[str, Any]:
        self.calls.append(("get", {"partition": partition}))
        return _partition_detail(name=partition)


class MissingPartitionConfigService:
    pass


def _build_app(service) -> FastAPI:
    app = FastAPI()
    app.include_router(partitions.router, prefix="/partition")
    app.dependency_overrides[require_partition_owner] = lambda: {"id": "admin", "is_admin": True}
    app.dependency_overrides[require_partition_viewer] = lambda: {"id": "admin", "is_admin": True}
    app.dependency_overrides[get_partition_service] = lambda: service
    return app


@pytest.mark.asyncio
async def test_update_partition_config_forwards_only_provided_fields(async_client_factory):
    service = FakePartitionConfigService()
    app = _build_app(service)

    async with async_client_factory(app) as client:
        response = await client.patch(
            "/partition/legal",
            json={
                "description": "Updated",
                "indexation_preset": "legal-index",
                "retrieval_preset": "legal-search",
            },
        )

    assert response.status_code == 200
    assert response.json()["indexation_preset"] == "legal-index"
    assert service.calls == [
        (
            "update",
            {
                "partition": "legal",
                "description": "Updated",
                "indexation_preset": "legal-index",
                "retrieval_preset": "legal-search",
            },
        )
    ]


@pytest.mark.asyncio
async def test_get_partition_config_returns_resolved_config(async_client_factory):
    service = FakePartitionConfigService()
    app = _build_app(service)

    async with async_client_factory(app) as client:
        response = await client.get("/partition/legal/config")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "legal"
    assert body["indexation_pipeline"]["chunking"]["name"] == "recursive_splitter"
    assert service.calls == [("get", {"partition": "legal"})]


@pytest.mark.asyncio
async def test_partition_config_routes_return_503_until_service_methods_exist(async_client_factory):
    app = _build_app(MissingPartitionConfigService())

    async with async_client_factory(app) as client:
        patch_response = await client.patch("/partition/legal", json={"description": "Updated"})
        get_response = await client.get("/partition/legal/config")

    assert patch_response.status_code == 503
    assert patch_response.json()["detail"] == "update_partition_config is not available."
    assert get_response.status_code == 503
    assert get_response.json()["detail"] == "get_partition_config is not available."
