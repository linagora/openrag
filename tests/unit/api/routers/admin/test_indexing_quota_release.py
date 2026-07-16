"""Issue #664 — every pre-dispatch exit must hand the quota slot back.

``check_user_file_quota`` reserves a slot *before* the route body runs, so
from that point on any early return is a path where an admitted upload never
becomes a file. Each one is covered here end-to-end through the real
dependency (not a stub), because a leak here is silent: it costs the user a
slot forever and the only symptom is a quota that mysteriously shrinks.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest
from api.dependencies.auth import current_user, require_partition_editor
from api.dependencies.files import validate_file_format, validate_file_id, validate_metadata
from api.error_handlers import register_error_handlers
from api.routers.admin.indexing import router as indexer_router
from core.utils.exceptions import OpenRAGError, PartitionNotFoundError
from di.providers import get_auth_service, get_config, get_indexing_service
from fastapi import FastAPI, UploadFile

USER = {"id": 42, "is_admin": False, "file_quota": 5, "file_count": 0}


class RecordingAuthService:
    """Tracks the reserve/release pair the real AuthService would perform."""

    def __init__(self, *, grant: bool = True) -> None:
        self.grant = grant
        self.reserved: list[int] = []
        self.released: list[int] = []

    async def reserve_file_slot(self, user_id: int, *, default_quota: int) -> int:
        if not self.grant:
            raise OpenRAGError("File quota exceeded.", code="FILE_QUOTA_EXCEEDED", status_code=403)
        self.reserved.append(user_id)
        return len(self.reserved)

    async def release_file_slot(self, user_id: int) -> None:
        self.released.append(user_id)

    # copy_file's source-partition check goes through these.
    @staticmethod
    def check_partition_access(**kwargs) -> bool:
        return True


class FakeIndexingService:
    def __init__(
        self,
        *,
        exists: bool = False,
        workspace: dict | None = None,
        add_error: Exception | None = None,
        copy_result: bool = True,
        copy_error: Exception | None = None,
    ) -> None:
        self.exists = exists
        self.workspace = workspace
        self.add_error = add_error
        self.copy_result = copy_result
        self.copy_error = copy_error
        self.dispatched = 0

    async def file_exists(self, file_id: str, partition: str) -> bool:
        return self.exists

    async def get_workspace(self, workspace_id: str):
        return self.workspace

    async def add_file(self, **kwargs):
        if self.add_error is not None:
            raise self.add_error
        self.dispatched += 1
        return "task-1"

    async def copy_file(self, **kwargs) -> bool:
        if self.copy_error is not None:
            raise self.copy_error
        return self.copy_result


def _no_metadata() -> dict:
    return {}


def _build_app(tmp_path, auth_service, service, *, content: bytes = b"hi"):
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(indexer_router, prefix="/indexer")

    cfg = SimpleNamespace(
        paths=SimpleNamespace(data_dir=str(tmp_path / "data")),
        rdb=SimpleNamespace(default_file_quota=5),
        server=SimpleNamespace(preferred_url_scheme="http"),
    )

    app.dependency_overrides[validate_file_id] = lambda: "f1"
    app.dependency_overrides[validate_file_format] = lambda: UploadFile(file=io.BytesIO(content), filename="doc.txt")
    app.dependency_overrides[validate_metadata] = _no_metadata
    app.dependency_overrides[require_partition_editor] = lambda: USER
    app.dependency_overrides[current_user] = lambda: USER
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_indexing_service] = lambda: service
    # check_user_file_quota itself is intentionally NOT overridden.
    return app


async def _post(app, url, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(url, **kwargs)


# ── add_file ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_dispatch_keeps_the_slot(tmp_path):
    auth = RecordingAuthService()
    service = FakeIndexingService()

    resp = await _post(_build_app(tmp_path, auth, service), "/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 201
    assert service.dispatched == 1
    assert auth.reserved == [42]
    assert auth.released == []


@pytest.mark.asyncio
async def test_quota_exceeded_rejects_before_touching_the_route(tmp_path):
    auth = RecordingAuthService(grant=False)
    service = FakeIndexingService()

    resp = await _post(_build_app(tmp_path, auth, service), "/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 403
    assert service.dispatched == 0
    assert auth.released == []


@pytest.mark.asyncio
async def test_duplicate_file_409_releases_the_slot(tmp_path):
    auth = RecordingAuthService()
    service = FakeIndexingService(exists=True)

    resp = await _post(_build_app(tmp_path, auth, service), "/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 409
    assert auth.released == [42]


@pytest.mark.asyncio
async def test_oversize_upload_releases_the_slot(tmp_path, monkeypatch):
    monkeypatch.setattr("api.dependencies.files._max_upload_size_bytes", lambda: 8)
    auth = RecordingAuthService()
    service = FakeIndexingService()
    app = _build_app(tmp_path, auth, service, content=b"x" * 100)

    resp = await _post(app, "/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 413
    assert auth.released == [42]


@pytest.mark.asyncio
async def test_bad_workspace_ids_400_releases_the_slot(tmp_path):
    auth = RecordingAuthService()
    service = FakeIndexingService()

    resp = await _post(
        _build_app(tmp_path, auth, service),
        "/indexer/partition/p1/file/f1",
        data={"workspace_ids": "not-json"},
    )

    assert resp.status_code == 400
    assert auth.released == [42]


@pytest.mark.asyncio
async def test_unknown_workspace_404_releases_the_slot(tmp_path):
    auth = RecordingAuthService()
    service = FakeIndexingService(workspace=None)

    resp = await _post(
        _build_app(tmp_path, auth, service),
        "/indexer/partition/p1/file/f1",
        data={"workspace_ids": '["ws-1"]'},
    )

    assert resp.status_code == 404
    assert auth.released == [42]


@pytest.mark.asyncio
async def test_dispatch_failure_releases_the_slot(tmp_path):
    """Anything raising between admission and a queued job gives the slot back."""
    auth = RecordingAuthService()
    service = FakeIndexingService(add_error=PartitionNotFoundError("Partition 'p1' does not exist."))

    resp = await _post(_build_app(tmp_path, auth, service), "/indexer/partition/p1/file/f1", data={"_": "1"})

    assert resp.status_code == 404
    assert service.dispatched == 0
    assert auth.released == [42]


# ── copy_file ──────────────────────────────────────────────────────────────


def _copy_app(tmp_path, auth, service):
    from api.dependencies.auth import current_user_partitions
    from di.providers import get_partition_service

    app = _build_app(tmp_path, auth, service)
    app.dependency_overrides[current_user_partitions] = lambda: [{"partition": "src", "role": "owner"}]
    app.dependency_overrides[get_partition_service] = lambda: SimpleNamespace()
    return app


@pytest.mark.asyncio
async def test_copy_file_is_quota_gated_and_keeps_the_slot_on_success(tmp_path):
    auth = RecordingAuthService()
    service = FakeIndexingService(copy_result=True)

    resp = await _post(
        _copy_app(tmp_path, auth, service),
        "/indexer/partition/p1/file/f1/copy",
        data={"source_partition": "src", "source_file_id": "src-f1"},
    )

    assert resp.status_code == 201
    assert auth.reserved == [42]
    assert auth.released == []


@pytest.mark.asyncio
async def test_copy_file_over_quota_is_rejected(tmp_path):
    """Regression: copy_file is the second quota-gated route (#664)."""
    auth = RecordingAuthService(grant=False)

    resp = await _post(
        _copy_app(tmp_path, auth, FakeIndexingService()),
        "/indexer/partition/p1/file/f1/copy",
        data={"source_partition": "src", "source_file_id": "src-f1"},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_copy_file_that_creates_no_row_releases_the_slot(tmp_path):
    """Empty source, or the target already existed — no file, no slot."""
    auth = RecordingAuthService()
    service = FakeIndexingService(copy_result=False)

    resp = await _post(
        _copy_app(tmp_path, auth, service),
        "/indexer/partition/p1/file/f1/copy",
        data={"source_partition": "src", "source_file_id": "src-f1"},
    )

    assert resp.status_code == 201
    assert auth.released == [42]


@pytest.mark.asyncio
async def test_copy_file_error_releases_the_slot(tmp_path):
    auth = RecordingAuthService()
    service = FakeIndexingService(copy_error=RuntimeError("milvus down"))

    with pytest.raises(RuntimeError, match="milvus down"):
        await _post(
            _copy_app(tmp_path, auth, service),
            "/indexer/partition/p1/file/f1/copy",
            data={"source_partition": "src", "source_file_id": "src-f1"},
        )

    assert auth.released == [42]
