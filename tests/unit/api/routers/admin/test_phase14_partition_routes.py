from __future__ import annotations

from typing import Any

import pytest
from api.dependencies.auth import (
    partitions_with_details,
    require_partition_owner,
    require_partition_viewer,
)
from api.routers.admin import partitions
from di.providers import get_partition_service
from fastapi import FastAPI, HTTPException, status


def _partition_detail(**overrides: Any) -> dict[str, Any]:
    """Build a resolved partition detail response row for router tests."""
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
    """Fake partition config service that records router calls."""

    def __init__(self) -> None:
        """Initialize the call log."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def update_partition_config(self, partition: str, **fields: Any) -> dict[str, Any]:
        """Record partition config updates and echo a response row."""
        self.calls.append(("update", {"partition": partition, **fields}))
        return _partition_detail(name=partition, **fields)

    async def get_partition_config(self, partition: str) -> dict[str, Any]:
        """Record partition config reads."""
        self.calls.append(("get", {"partition": partition}))
        return _partition_detail(name=partition)


class MissingPartitionConfigService:
    """Service double without Phase 14 config methods."""


def _build_app(service) -> FastAPI:
    """Build a small app with partition routes and fake dependencies."""
    app = FastAPI()
    app.include_router(partitions.router, prefix="/partition")
    app.dependency_overrides[require_partition_owner] = lambda: {"id": "admin", "is_admin": True}
    app.dependency_overrides[require_partition_viewer] = lambda: {"id": "admin", "is_admin": True}
    app.dependency_overrides[get_partition_service] = lambda: service
    return app


class FakeListService:
    """Service double whose summaries cover the full set of partitions."""

    async def list_partitions(self) -> list[dict[str, Any]]:
        """Return every partition (the admin all-expansion target)."""
        return [{"partition": "p1"}, {"partition": "p2"}]

    async def list_partition_summaries(self) -> dict[str, dict[str, Any]]:
        """Summaries keyed by name — the admin-UI enrichment source the
        list route reads from for the all-expansion."""
        return {
            "p1": {"partition": "p1", "document_count": 0},
            "p2": {"partition": "p2", "document_count": 0},
        }


class FakeCreatePartitionService:
    """Service double for create-partition route tests."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def partition_exists(self, partition: str) -> bool:
        return False

    async def create_partition(self, **kwargs: Any) -> None:
        self.created.append(kwargs)


class FakeMemberCandidateService:
    """Service double for the partition member candidate route."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_member_candidates(
        self,
        partition: str,
        *,
        search: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "partition": partition,
                "search": search,
                "offset": offset,
                "limit": limit,
            }
        )
        return {
            "candidates": [
                {"user_id": 2, "display_name": "Sam"},
                {"user_id": 3, "display_name": "Sam"},
            ],
            "offset": offset,
            "limit": limit,
            "has_more": True,
            "next_offset": offset + limit,
        }


def _build_list_app(*, is_admin: bool) -> FastAPI:
    """App for the list route, with a regular user whose sole membership is ``all``."""
    app = FastAPI()

    @app.middleware("http")
    async def _set_user(request, call_next):
        request.state.user = {"id": 1, "is_admin": is_admin}
        return await call_next(request)

    app.include_router(partitions.router, prefix="/partition")
    app.dependency_overrides[partitions_with_details] = lambda: [{"partition": "all"}]
    app.dependency_overrides[get_partition_service] = lambda: FakeListService()
    return app


def _build_create_app(service, *, is_admin: bool = False) -> FastAPI:
    """App for create route tests with a current user in request state."""
    app = FastAPI()

    @app.middleware("http")
    async def _set_user(request, call_next):
        request.state.user = {"id": 1, "is_admin": is_admin}
        return await call_next(request)

    app.include_router(partitions.router, prefix="/partition")
    app.dependency_overrides[get_partition_service] = lambda: service
    return app


@pytest.mark.asyncio
async def test_list_partitions_expands_all_only_for_admin(async_client_factory):
    """An admin's ``all`` sentinel expands to every partition."""
    app = _build_list_app(is_admin=True)
    async with async_client_factory(app) as client:
        response = await client.get("/partition/")
    assert response.status_code == 200
    assert [p["partition"] for p in response.json()["partitions"]] == ["p1", "p2"]


@pytest.mark.asyncio
async def test_list_partitions_does_not_expand_all_for_non_admin(async_client_factory):
    """A regular user whose only membership is a partition named ``all`` must not
    receive every partition — the all-expansion is gated on is_admin."""
    app = _build_list_app(is_admin=False)
    async with async_client_factory(app) as client:
        response = await client.get("/partition/")
    assert response.status_code == 200
    assert [p["partition"] for p in response.json()["partitions"]] == ["all"]


@pytest.mark.asyncio
async def test_list_partition_member_candidates_returns_stable_identities(async_client_factory):
    service = FakeMemberCandidateService()
    app = _build_app(service)

    async with async_client_factory(app) as client:
        response = await client.get(
            "/partition/legal/users/candidates",
            params={"search": "sam", "offset": 20, "limit": 10},
        )

    assert response.status_code == 200
    assert response.json() == {
        "candidates": [
            {"user_id": 2, "display_name": "Sam"},
            {"user_id": 3, "display_name": "Sam"},
        ],
        "offset": 20,
        "limit": 10,
        "has_more": True,
        "next_offset": 30,
    }
    assert service.calls == [
        {
            "partition": "legal",
            "search": "sam",
            "offset": 20,
            "limit": 10,
        }
    ]


@pytest.mark.asyncio
async def test_list_partition_member_candidates_rejects_non_owner(async_client_factory):
    service = FakeMemberCandidateService()
    app = _build_app(service)

    async def reject_non_owner():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Partition owner role required",
        )

    app.dependency_overrides[require_partition_owner] = reject_non_owner

    async with async_client_factory(app) as client:
        response = await client.get("/partition/legal/users/candidates")

    assert response.status_code == 403
    assert service.calls == []


@pytest.mark.asyncio
async def test_update_partition_config_forwards_only_provided_fields(async_client_factory):
    """Partition config updates should exclude omitted fields."""
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
    """Partition config reads should return the resolved service payload."""
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
async def test_partition_config_routes_return_501_until_service_methods_exist(async_client_factory):
    """Missing phased service methods should return not-implemented responses."""
    app = _build_app(MissingPartitionConfigService())

    async with async_client_factory(app) as client:
        patch_response = await client.patch("/partition/legal", json={"description": "Updated"})
        get_response = await client.get("/partition/legal/config")

    assert patch_response.status_code == 501
    assert patch_response.json()["detail"] == "update_partition_config is not available."
    assert get_response.status_code == 501
    assert get_response.json()["detail"] == "get_partition_config is not available."


@pytest.mark.asyncio
async def test_create_partition_rejects_malformed_limit_before_writing(async_client_factory, monkeypatch):
    """A bad deployment env value should fail cleanly before creating a partition."""
    monkeypatch.setenv("MAX_PARTITIONS_PER_USER", "not-an-int")
    service = FakeCreatePartitionService()
    app = _build_create_app(service)

    async with async_client_factory(app) as client:
        response = await client.post("/partition/legal")

    assert response.status_code == 500
    assert "MAX_PARTITIONS_PER_USER" in response.json()["detail"]
    assert service.created == []
