"""PATCH /indexer/partition/{partition}/file/{file_id} updates metadata and
never moves the file to another partition."""

from unittest.mock import MagicMock

import httpx
import pytest
from api.dependencies.auth import require_partition_editor
from api.dependencies.files import validate_file_id, validate_metadata
from api.error_handlers import register_error_handlers
from api.routers.admin.indexing import router as indexer_router
from di.providers import get_indexing_service
from fastapi import FastAPI
from services.orchestrators.indexing_service import IndexingService


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.updated: list[tuple] = []

    async def update_file_metadata(self, file_id, metadata, partition, user):
        self.updated.append((file_id, metadata, partition, user))


async def _patch(metadata: dict) -> tuple[httpx.Response, _RecordingDispatcher]:
    dispatcher = _RecordingDispatcher()
    service = IndexingService(document_repo=MagicMock(), workspace_repo=MagicMock(), dispatcher=dispatcher)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(indexer_router, prefix="/indexer")
    app.dependency_overrides[validate_file_id] = lambda: "f1"
    app.dependency_overrides[validate_metadata] = lambda: metadata
    app.dependency_overrides[require_partition_editor] = lambda: {"id": 7, "is_admin": False}
    app.dependency_overrides[get_indexing_service] = lambda: service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch("/indexer/partition/p1/file/f1", data={"_": "1"})
    return response, dispatcher


@pytest.mark.asyncio
async def test_patch_updates_the_metadata():
    response, dispatcher = await _patch({"title": "new"})

    assert response.status_code == 200
    assert dispatcher.updated == [("f1", {"title": "new", "file_id": "f1"}, "p1", {"id": 7, "is_admin": False})]


@pytest.mark.asyncio
async def test_patch_naming_the_file_partition_updates_the_metadata():
    response, dispatcher = await _patch({"title": "new", "partition": "p1"})

    assert response.status_code == 200
    assert len(dispatcher.updated) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["p2", "missing", "all"])
async def test_patch_refuses_another_partition_and_writes_nothing(target):
    response, dispatcher = await _patch({"title": "new", "partition": target})

    assert response.status_code == 400
    assert response.json()["detail"].startswith("[PARTITION_CHANGE_UNSUPPORTED]: ")
    assert dispatcher.updated == []
