from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from api.dependencies.auth import require_task_owner
from api.routers.admin import indexing
from core.config.infrastructure import PathsConfig
from core.config.root import Settings
from di.providers import get_config, get_indexing_service
from fastapi import FastAPI


class FakeIndexingService:
    async def get_task_state(self, task_id: str) -> str:
        return "SERIALIZING"

    async def get_task_info(self, task_id: str) -> dict[str, Any]:
        return {
            "state": "SERIALIZING",
            "current_stage": "EMBEDDING",
            "failed_stage": None,
            "stage_durations": {"parsing_seconds": 2.5, "chunking_seconds": 0.1},
            "stage_history": [{"stage": "PARSING", "duration_seconds": 2.5}],
            "details": {"file_id": "doc-1", "partition": "tenant-a", "user_id": 7},
        }


def _build_app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(indexing.router, prefix="/indexer")
    app.dependency_overrides[get_config] = lambda: Settings(paths=PathsConfig(data_dir=tmp_path))
    app.dependency_overrides[get_indexing_service] = lambda: FakeIndexingService()
    app.dependency_overrides[require_task_owner] = lambda: {
        "file_id": "doc-1",
        "partition": "tenant-a",
        "user_id": 7,
    }
    return app


@pytest.mark.asyncio
async def test_task_status_exposes_current_stage(async_client_factory, tmp_path):
    app = _build_app(tmp_path)

    async with async_client_factory(app) as client:
        response = await client.get("/indexer/task/task-1")

    assert response.status_code == 200
    assert response.json() == {
        "task_id": "task-1",
        "task_state": "SERIALIZING",
        "current_stage": "EMBEDDING",
        "failed_stage": None,
        "stage_durations": {"parsing_seconds": 2.5, "chunking_seconds": 0.1},
        "stage_history": [{"stage": "PARSING", "duration_seconds": 2.5}],
        "details": {"file_id": "doc-1", "partition": "tenant-a", "user_id": 7},
    }
