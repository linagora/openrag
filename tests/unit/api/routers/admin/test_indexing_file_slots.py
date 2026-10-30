"""The quota-gated routes reserve a file slot and settle it on every exit (#664).

These run the real ``check_user_file_quota`` through FastAPI, so they also pin
that the route's exception reaches the dependency's teardown.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest
from api.dependencies.auth import current_user, current_user_partitions, require_partition_editor
from api.dependencies.files import validate_file_format, validate_file_id, validate_metadata
from api.error_handlers import register_error_handlers
from api.routers.admin import indexing as indexing_routes
from core.models.user import FileSlotGrant
from core.utils.exceptions import ConflictError
from di.providers import get_auth_service, get_config, get_indexing_service, get_job_service, get_partition_service
from fastapi import FastAPI, UploadFile
from services.orchestrators.auth_service import AuthService

_CAPPED = {"id": 7, "is_admin": False, "file_quota": 5, "file_count": 1}
_UNLIMITED = {"id": 7, "is_admin": False, "file_quota": -1, "file_count": 1}


class _SlotRepo:
    def __init__(self, grant: FileSlotGrant = FileSlotGrant.RESERVED) -> None:
        self.grant = grant
        self.reserved: list[str] = []
        self.released: list[str] = []

    async def try_reserve_file_slot(self, user_id, reservation_id, *, default_quota):
        self.reserved.append(reservation_id)
        return self.grant

    async def release_file_slot(self, reservation_id):
        self.released.append(reservation_id)
        return True


class _IndexingService:
    def __init__(self, *, exists: bool = False, error: BaseException | None = None, workspace: bool = True) -> None:
        self.exists = exists
        self.error = error
        self.workspace = workspace
        self.add_calls: list[dict] = []
        self.copy_calls: list[dict] = []

    async def file_exists(self, file_id: str, partition: str) -> bool:
        return self.exists

    async def get_workspace(self, partition: str, workspace_id: str):
        return {"workspace_id": workspace_id} if self.workspace else None

    async def add_file(self, **kwargs):
        self.add_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return kwargs.get("task_id") or "minted-task"

    async def copy_file(self, **kwargs):
        self.copy_calls.append(kwargs)
        if self.error is not None:
            raise self.error


class _JobService:
    async def get_task_details(self, task_id: str):
        return {"task_id": task_id, "user_id": 7}


def _no_metadata() -> dict:
    return {}


def _no_partitions() -> list:
    return []


def _build_app(tmp_path, monkeypatch, *, repo, service, user=_CAPPED, content=b"data") -> FastAPI:
    monkeypatch.setattr("api.dependencies.files._max_upload_size_bytes", lambda: 8)

    async def _allow(**_kwargs):
        return True

    monkeypatch.setattr(indexing_routes, "ensure_partition_role", _allow)

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(indexing_routes.router, prefix="/indexer")
    cfg = SimpleNamespace(
        paths=SimpleNamespace(data_dir=str(tmp_path / "data")),
        loader=SimpleNamespace(content_deduplication_enabled=False),
        server=SimpleNamespace(preferred_url_scheme="http"),
        rdb=SimpleNamespace(default_file_quota=10),
    )
    auth_service = AuthService(
        user_repo=repo,
        oidc_session_repo=object(),
        membership_repo=object(),
        oidc_client=None,
        config=SimpleNamespace(),
    )
    app.dependency_overrides[validate_file_id] = lambda: "f1"
    app.dependency_overrides[validate_file_format] = lambda: UploadFile(file=io.BytesIO(content), filename="a.txt")
    app.dependency_overrides[validate_metadata] = _no_metadata
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[current_user_partitions] = _no_partitions
    app.dependency_overrides[require_partition_editor] = lambda: user
    app.dependency_overrides[get_auth_service] = lambda: auth_service
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_indexing_service] = lambda: service
    app.dependency_overrides[get_job_service] = lambda: _JobService()
    app.dependency_overrides[get_partition_service] = lambda: object()
    return app


async def _send(app: FastAPI, method: str, path: str, data: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, data=data or {"_": "1"})


# ── add_file ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_queued_upload_runs_under_its_reservation_and_keeps_it(tmp_path, monkeypatch):
    repo, service = _SlotRepo(), _IndexingService()
    app = _build_app(tmp_path, monkeypatch, repo=repo, service=service)

    resp = await _send(app, "POST", "/indexer/partition/p1/file/f1")

    assert resp.status_code == 201
    (reservation_id,) = repo.reserved
    assert service.add_calls[0]["task_id"] == reservation_id
    assert resp.json()["task_status_url"].endswith(f"/task/{reservation_id}")
    # Handed off: the completion tracker releases it if the task ends without a file.
    assert repo.released == []


@pytest.mark.parametrize(
    ("service", "content", "data", "status"),
    [
        (_IndexingService(exists=True), b"data", None, 409),
        (_IndexingService(), b"x" * 100, None, 413),
        (_IndexingService(workspace=False), b"data", {"workspace_ids": '["ws-1"]'}, 404),
        (_IndexingService(), b"data", {"workspace_ids": "not-json"}, 400),
        (
            _IndexingService(
                error=ConflictError(
                    "File 'f1' is already being indexed in partition 'p1'.",
                    code="DOCUMENT_INDEXING_IN_PROGRESS",
                    existing_task_id="task-running",
                )
            ),
            b"data",
            None,
            409,
        ),
        (_IndexingService(error=RuntimeError("dispatcher unavailable")), b"data", None, 500),
    ],
    ids=["file-exists", "upload-too-large", "unknown-workspace", "bad-workspace-ids", "already-indexing", "dispatch"],
)
@pytest.mark.asyncio
async def test_an_upload_that_is_never_queued_gives_its_slot_back(
    tmp_path, monkeypatch, service, content, data, status
):
    repo = _SlotRepo()
    app = _build_app(tmp_path, monkeypatch, repo=repo, service=service, content=content)

    resp = await _send(app, "POST", "/indexer/partition/p1/file/f1", data=data)

    assert resp.status_code == status
    assert len(repo.reserved) == 1
    assert repo.released == repo.reserved


@pytest.mark.asyncio
async def test_a_full_quota_rejects_the_upload_before_it_is_saved(tmp_path, monkeypatch):
    repo, service = _SlotRepo(FileSlotGrant.DENIED), _IndexingService()
    app = _build_app(tmp_path, monkeypatch, repo=repo, service=service)

    resp = await _send(app, "POST", "/indexer/partition/p1/file/f1")

    assert resp.status_code == 403
    assert service.add_calls == []
    assert not (tmp_path / "data").exists() or list((tmp_path / "data").iterdir()) == []
    assert repo.released == []


@pytest.mark.asyncio
async def test_an_unlimited_upload_reserves_nothing(tmp_path, monkeypatch):
    repo, service = _SlotRepo(), _IndexingService()
    app = _build_app(tmp_path, monkeypatch, repo=repo, service=service, user=_UNLIMITED)

    resp = await _send(app, "POST", "/indexer/partition/p1/file/f1")

    assert resp.status_code == 201
    assert service.add_calls[0]["task_id"] is None
    assert repo.reserved == []


@pytest.mark.asyncio
async def test_a_replace_reserves_nothing(tmp_path, monkeypatch):
    """A re-index reuses the file's row, so it holds no slot."""
    repo, service = _SlotRepo(), _IndexingService(exists=True)
    app = _build_app(tmp_path, monkeypatch, repo=repo, service=service)

    resp = await _send(app, "PUT", "/indexer/partition/p1/file/f1")

    assert resp.status_code == 202
    assert "task_id" not in service.add_calls[0]
    assert repo.reserved == []


# ── copy_file ────────────────────────────────────────────────────────


_COPY_FORM = {"source_partition": "p0", "source_file_id": "f0"}


@pytest.mark.asyncio
async def test_a_copy_hands_its_reservation_to_the_catalog_insert(tmp_path, monkeypatch):
    repo, service = _SlotRepo(), _IndexingService()
    app = _build_app(tmp_path, monkeypatch, repo=repo, service=service)

    resp = await _send(app, "POST", "/indexer/partition/p1/file/f1/copy", data=_COPY_FORM)

    assert resp.status_code == 201
    (reservation_id,) = repo.reserved
    assert service.copy_calls[0]["quota_reservation_id"] == reservation_id
    # Released on the way out regardless: a no-op once the insert consumed it,
    # and the slot back when the copy wrote no row.
    assert repo.released == [reservation_id]


@pytest.mark.asyncio
async def test_a_failed_copy_gives_its_slot_back(tmp_path, monkeypatch):
    repo = _SlotRepo()
    service = _IndexingService(
        error=ConflictError("exists", code="DOCUMENT_CONTENT_EXISTS", existing_file_id="other"),
    )
    app = _build_app(tmp_path, monkeypatch, repo=repo, service=service)

    resp = await _send(app, "POST", "/indexer/partition/p1/file/f1/copy", data=_COPY_FORM)

    assert resp.status_code == 409
    assert repo.released == repo.reserved != []


@pytest.mark.asyncio
async def test_a_copy_over_quota_is_refused(tmp_path, monkeypatch):
    repo, service = _SlotRepo(FileSlotGrant.DENIED), _IndexingService()
    app = _build_app(tmp_path, monkeypatch, repo=repo, service=service)

    resp = await _send(app, "POST", "/indexer/partition/p1/file/f1/copy", data=_COPY_FORM)

    assert resp.status_code == 403
    assert service.copy_calls == []
