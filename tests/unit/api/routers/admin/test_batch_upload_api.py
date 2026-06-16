from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from api.dependencies.auth import check_user_file_quota, require_partition_editor
from api.routers.admin import indexing
from core.config.infrastructure import PathsConfig
from core.config.root import Settings
from di.providers import get_config, get_indexing_service
from fastapi import FastAPI


class FakeIndexingService:
    """Fake indexing service that records batch upload dispatches."""

    def __init__(self, *, existing_files: set[str] | None = None) -> None:
        self.existing_files = existing_files or set()
        self.calls: list[dict[str, Any]] = []

    async def file_exists(self, file_id: str, partition: str) -> bool:
        """Return whether the file exists in the fake partition."""
        return file_id in self.existing_files

    async def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        """Return a workspace belonging to the requested test partition."""
        return {"id": workspace_id, "partition_name": "batch-partition"}

    async def add_file(self, **kwargs: Any) -> str:
        """Record the queued file and return a stable fake task id."""
        self.calls.append(kwargs)
        return f"task-{kwargs['file_id']}"


def _build_app(tmp_path: Path, service: FakeIndexingService) -> FastAPI:
    app = FastAPI()
    app.include_router(indexing.router, prefix="/indexer")
    settings = Settings(paths=PathsConfig(data_dir=tmp_path))
    app.dependency_overrides[get_config] = lambda: settings
    app.dependency_overrides[get_indexing_service] = lambda: service
    app.dependency_overrides[require_partition_editor] = lambda: {"id": 1, "is_admin": True}
    app.dependency_overrides[check_user_file_quota] = lambda: {"id": 1, "is_admin": True}
    return app


@pytest.mark.asyncio
async def test_batch_upload_returns_partial_success(async_client_factory, tmp_path):
    """Batch upload should accept valid files while reporting failed entries."""
    service = FakeIndexingService(existing_files={"duplicate-file"})
    app = _build_app(tmp_path, service)

    items = [
        {"file_id": "new-file", "metadata": {"category": "docs"}},
        {"file_id": "duplicate-file", "metadata": {"category": "docs"}},
    ]

    async with async_client_factory(app) as client:
        response = await client.post(
            "/indexer/partition/batch-partition/files",
            data={"items": json.dumps(items)},
            files=[
                ("files", ("new.txt", b"new content", "text/plain")),
                ("files", ("duplicate.txt", b"duplicate content", "text/plain")),
            ],
        )

    assert response.status_code == 207
    assert response.json()["accepted"] == 1
    assert response.json()["failed"] == 1
    assert response.json()["results"] == [
        {
            "file_id": "new-file",
            "status": "accepted",
            "task_status_url": "http://testserver/indexer/task/task-new-file",
            "detail": None,
        },
        {
            "file_id": "duplicate-file",
            "status": "failed",
            "task_status_url": None,
            "detail": "File 'duplicate-file' already exists in partition batch-partition",
        },
    ]
    assert [call["file_id"] for call in service.calls] == ["new-file"]
    assert service.calls[0]["metadata"] == {"category": "docs"}
    assert service.calls[0]["sanitized_filename"] == "new.txt"
    assert service.calls[0]["original_filename"] == "new.txt"


@pytest.mark.asyncio
async def test_batch_upload_rejects_mismatched_files_and_items(async_client_factory, tmp_path):
    """A malformed batch should fail before any file is queued."""
    service = FakeIndexingService()
    app = _build_app(tmp_path, service)

    async with async_client_factory(app) as client:
        response = await client.post(
            "/indexer/partition/batch-partition/files",
            data={"items": json.dumps([{"file_id": "only-one", "metadata": {}}])},
            files=[
                ("files", ("first.txt", b"first", "text/plain")),
                ("files", ("second.txt", b"second", "text/plain")),
            ],
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "items must contain exactly one entry for each uploaded file"
    assert service.calls == []
