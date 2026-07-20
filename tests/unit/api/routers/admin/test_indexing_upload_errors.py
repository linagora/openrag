"""The add-file route must surface upload-rejection status codes.

``save_file_to_disk`` raises ``ValidationError`` (e.g. 413 for an oversize
upload) which is an ``OpenRAGError``. Regression guard: the route's broad
``except Exception`` must not mask that as a 500 — the OpenRAGError handler
should map it to its real status.
"""

import io
from types import SimpleNamespace

import httpx
import pytest
from api.dependencies.auth import check_user_file_quota, require_partition_editor
from api.dependencies.files import validate_file_format, validate_file_id, validate_metadata
from api.error_handlers import register_error_handlers
from api.routers.admin.indexing import router as indexer_router
from core.utils.exceptions import ConflictError
from di.providers import get_config, get_indexing_service
from fastapi import FastAPI, UploadFile


class _FakeIndexingService:
    async def file_exists(self, file_id: str, partition: str) -> bool:
        return False

    async def add_file(self, **_kwargs):
        raise ConflictError(
            "This document already exists in partition 'p1'.",
            code="DOCUMENT_CONTENT_EXISTS",
            existing_file_id="existing-file",
        )


def _empty_metadata() -> dict:
    return {}


def _build_app(tmp_path, monkeypatch, content: bytes) -> FastAPI:
    # Cap at ~8 bytes so a small upload trips the limit.
    monkeypatch.setattr("api.dependencies.files._max_upload_size_bytes", lambda: 8)

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(indexer_router, prefix="/indexer")

    cfg = SimpleNamespace(paths=SimpleNamespace(data_dir=str(tmp_path / "data")))

    app.dependency_overrides[validate_file_id] = lambda: "f1"
    app.dependency_overrides[validate_file_format] = lambda: UploadFile(file=io.BytesIO(content), filename="big.bin")
    app.dependency_overrides[validate_metadata] = _empty_metadata
    app.dependency_overrides[require_partition_editor] = lambda: {"id": 1, "is_admin": True}
    app.dependency_overrides[check_user_file_quota] = lambda: None
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_indexing_service] = lambda: _FakeIndexingService()
    return app


@pytest.mark.asyncio
async def test_add_file_oversize_returns_413_not_500(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, content=b"x" * 100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_add_file_duplicate_content_returns_409_and_removes_upload(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, content=b"same")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 409
    assert resp.json()["extra"]["existing_file_id"] == "existing-file"
    assert list((tmp_path / "data").iterdir()) == []
