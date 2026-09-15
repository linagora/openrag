"""Routes for moving a partition to another embedder (#762 F4)."""

from __future__ import annotations

from typing import Any

import pytest
from api.dependencies.auth import require_partition_owner, require_partition_viewer
from api.error_handlers import register_error_handlers
from api.routers.admin import partitions
from core.utils.exceptions import ConflictError
from di.providers import get_embedder_swap_service
from fastapi import FastAPI, HTTPException, status


def _swap(**overrides: Any) -> dict[str, Any]:
    row = {
        "partition": "legal",
        "source_embedder": "e5",
        "target_embedder": "bge-m3",
        "status": "running",
        "files_total": 4,
        "files_done": 0,
        "error": None,
        "started_at": "2026-09-14T00:00:00+00:00",
        "updated_at": "2026-09-14T00:00:00+00:00",
        "finished_at": None,
    }
    row.update(overrides)
    return row


class FakeSwapService:
    def __init__(self, *, start_error: Exception | None = None, swapped: bool = True) -> None:
        self.calls: list[tuple] = []
        self.start_error = start_error
        self.swapped = swapped

    async def start(self, partition: str, embedder: str) -> dict:
        self.calls.append(("start", partition, embedder))
        if self.start_error is not None:
            raise self.start_error
        return _swap(partition=partition, target_embedder=embedder)

    async def get(self, partition: str) -> dict | None:
        self.calls.append(("get", partition))
        return _swap(partition=partition, files_done=2) if self.swapped else None

    async def cancel(self, partition: str) -> dict:
        self.calls.append(("cancel", partition))
        return _swap(partition=partition, status="cancelled")


def _deny() -> None:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="owner required")


def _build_app(service: FakeSwapService, *, owner: bool = True) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(partitions.router, prefix="/partition")
    app.dependency_overrides[require_partition_owner] = (lambda: {"id": 1}) if owner else _deny
    app.dependency_overrides[require_partition_viewer] = lambda: {"id": 1}
    app.dependency_overrides[get_embedder_swap_service] = lambda: service
    return app


@pytest.mark.asyncio
async def test_starting_a_swap_is_accepted_and_returns_it(async_client_factory):
    service = FakeSwapService()
    async with async_client_factory(_build_app(service)) as client:
        response = await client.post("/partition/legal/embedder-swap", json={"embedder": "  bge-m3 "})

    assert response.status_code == 202
    assert response.json()["target_embedder"] == "bge-m3"
    assert service.calls == [("start", "legal", "bge-m3")]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"embedder": "   "}, {"embedder": "bge-m3", "force": True}])
async def test_a_start_without_a_usable_embedder_name_is_rejected(async_client_factory, body):
    service = FakeSwapService()
    async with async_client_factory(_build_app(service)) as client:
        response = await client.post("/partition/legal/embedder-swap", json=body)

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.asyncio
async def test_a_refused_start_is_a_conflict(async_client_factory):
    service = FakeSwapService(start_error=ConflictError("already running", code="EMBEDDER_SWAP_IN_PROGRESS"))
    async with async_client_factory(_build_app(service)) as client:
        response = await client.post("/partition/legal/embedder-swap", json={"embedder": "bge-m3"})

    assert response.status_code == 409
    assert "EMBEDDER_SWAP_IN_PROGRESS" in response.text


@pytest.mark.asyncio
async def test_progress_is_readable_by_a_viewer(async_client_factory):
    service = FakeSwapService()
    async with async_client_factory(_build_app(service, owner=False)) as client:
        response = await client.get("/partition/legal/embedder-swap")

    assert response.status_code == 200
    assert response.json()["files_done"] == 2


@pytest.mark.asyncio
async def test_a_partition_that_never_swapped_reads_as_null_not_an_error(async_client_factory):
    service = FakeSwapService(swapped=False)
    async with async_client_factory(_build_app(service, owner=False)) as client:
        response = await client.get("/partition/legal/embedder-swap")

    assert response.status_code == 200
    assert response.json() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "kwargs"),
    [("post", {"json": {"embedder": "bge-m3"}}), ("delete", {})],
)
async def test_starting_and_cancelling_need_the_owner(async_client_factory, method, kwargs):
    service = FakeSwapService()
    async with async_client_factory(_build_app(service, owner=False)) as client:
        response = await getattr(client, method)("/partition/legal/embedder-swap", **kwargs)

    assert response.status_code == 403
    assert service.calls == []


@pytest.mark.asyncio
async def test_cancelling_returns_the_cancelled_swap(async_client_factory):
    service = FakeSwapService()
    async with async_client_factory(_build_app(service)) as client:
        response = await client.delete("/partition/legal/embedder-swap")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert service.calls == [("cancel", "legal")]
