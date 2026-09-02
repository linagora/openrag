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
from core.utils.exceptions import ConflictError, mark_indexing_worker_may_be_running
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

    async def get_workspace(self, _workspace_id: str):
        return None


class _DispatchFailureService(_FakeIndexingService):
    async def add_file(self, **_kwargs):
        raise RuntimeError("dispatcher unavailable")


class _ExistingFileService(_FakeIndexingService):
    def __init__(self) -> None:
        super().__init__()
        self.dispatched = False

    async def file_exists(self, file_id: str, partition: str) -> bool:
        return True

    async def add_file(self, **_kwargs):
        self.dispatched = True
        return "unexpected-task"


def _empty_metadata() -> dict:
    return {}


def _build_app(tmp_path, monkeypatch, content: bytes, *, service=None, deduplication_enabled=True) -> FastAPI:
    # Cap at ~8 bytes so a small upload trips the limit.
    monkeypatch.setattr("api.dependencies.files._max_upload_size_bytes", lambda: 8)

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(indexer_router, prefix="/indexer")

    cfg = SimpleNamespace(
        paths=SimpleNamespace(data_dir=str(tmp_path / "data")),
        loader=SimpleNamespace(content_deduplication_enabled=deduplication_enabled),
        server=SimpleNamespace(preferred_url_scheme="http"),
    )

    app.dependency_overrides[validate_file_id] = lambda: "f1"
    app.dependency_overrides[validate_file_format] = lambda: UploadFile(file=io.BytesIO(content), filename="big.bin")
    app.dependency_overrides[validate_metadata] = _empty_metadata
    app.dependency_overrides[require_partition_editor] = lambda: {"id": 1, "is_admin": True}
    app.dependency_overrides[check_user_file_quota] = lambda: None
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_indexing_service] = lambda: service or _FakeIndexingService()
    return app


@pytest.mark.asyncio
async def test_add_file_oversize_returns_413_not_500(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, content=b"x" * 100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_put_file_oversize_returns_413_not_500(tmp_path, monkeypatch):
    app = _build_app(
        tmp_path,
        monkeypatch,
        content=b"x" * 100,
        service=_ExistingFileService(),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.put("/indexer/partition/p1/file/f1", data={"_": "1"})

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


@pytest.mark.asyncio
async def test_add_file_invalid_workspace_is_rejected_before_upload_is_saved(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, content=b"same")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/indexer/partition/p1/file/f1",
            data={"workspace_ids": '["missing"]'},
        )

    assert resp.status_code == 404
    assert not (tmp_path / "data").exists()


@pytest.mark.asyncio
async def test_add_file_dispatch_failure_removes_upload(tmp_path, monkeypatch):
    app = _build_app(
        tmp_path,
        monkeypatch,
        content=b"same",
        service=_DispatchFailureService(),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 500
    assert list((tmp_path / "data").iterdir()) == []


@pytest.mark.parametrize(
    ("deduplication_enabled", "save_function"),
    [
        (True, "save_file_to_disk_with_sha256"),
        (False, "save_file_to_disk"),
    ],
)
@pytest.mark.asyncio
async def test_put_file_save_failure_returns_safe_error(
    tmp_path,
    monkeypatch,
    deduplication_enabled,
    save_function,
):
    async def fail_save(*_args, **_kwargs):
        raise OSError("private path: /srv/openrag/data")

    monkeypatch.setattr(f"api.routers.admin.indexing.{save_function}", fail_save)
    service = _ExistingFileService()
    app = _build_app(
        tmp_path,
        monkeypatch,
        content=b"replacement",
        service=service,
        deduplication_enabled=deduplication_enabled,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.put("/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 500
    assert resp.json() == {"detail": "Failed to save uploaded file."}
    assert "private path" not in resp.text
    assert service.dispatched is False


class _UnknownOutcomeService(_FakeIndexingService):
    """Dispatcher gave up on the submit RPC but a worker may already be live."""

    async def add_file(self, **_kwargs):
        exc = TimeoutError("submit timed out")
        mark_indexing_worker_may_be_running(exc)
        raise exc


class _UnknownOutcomePutService(_ExistingFileService):
    async def add_file(self, **_kwargs):
        exc = TimeoutError("submit timed out")
        mark_indexing_worker_may_be_running(exc)
        raise exc


@pytest.mark.asyncio
async def test_add_file_keeps_upload_when_worker_may_still_be_running(tmp_path, monkeypatch):
    """A worker launched before the submit response was lost still needs the file.

    ``IndexerPool.submit`` starts ``process_file`` before it returns, and the
    worker does not read the path until after its ref registration, so deleting
    the upload here would fail an indexing run that could still succeed.
    """
    app = _build_app(tmp_path, monkeypatch, content=b"same", service=_UnknownOutcomeService())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 500
    assert len(list((tmp_path / "data").iterdir())) == 1


@pytest.mark.asyncio
async def test_put_file_keeps_upload_when_worker_may_still_be_running(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch, content=b"same", service=_UnknownOutcomePutService())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.put("/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 500
    assert len(list((tmp_path / "data").iterdir())) == 1
