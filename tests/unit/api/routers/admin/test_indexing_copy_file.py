"""Copy requests must reject a missing source before creating a destination."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from api.dependencies.auth import check_user_file_quota, current_user_partitions, require_partition_editor
from api.error_handlers import register_error_handlers
from api.routers.admin.indexing import router as indexer_router
from core.utils.exceptions import StorageError
from di.providers import get_auth_service, get_indexing_service, get_partition_service
from fastapi import FastAPI
from services.orchestrators.auth_service import AuthService
from services.orchestrators.indexing_service import IndexingService


@pytest.fixture
def copy_app():
    document_repo = SimpleNamespace(file_exists_in_partition=AsyncMock(return_value=True))
    dispatcher = SimpleNamespace(copy_file=AsyncMock())
    partitions = SimpleNamespace(
        partition_exists=AsyncMock(return_value=False),
        create_partition=AsyncMock(),
        pin_embedder_for_write=AsyncMock(),
    )
    service = IndexingService(
        document_repo=document_repo,
        workspace_repo=MagicMock(),
        dispatcher=dispatcher,
        config=SimpleNamespace(partitions={"source": object()}),
        partition_service=partitions,
    )
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(indexer_router, prefix="/indexer")
    app.dependency_overrides[require_partition_editor] = lambda: {"id": 7, "is_admin": False}
    app.dependency_overrides[current_user_partitions] = lambda: [{"partition": "source", "role": "viewer"}]
    app.dependency_overrides[check_user_file_quota] = lambda: None
    app.dependency_overrides[get_auth_service] = lambda: AuthService
    app.dependency_overrides[get_partition_service] = lambda: partitions
    app.dependency_overrides[get_indexing_service] = lambda: service
    return app, document_repo, dispatcher, partitions


async def _copy(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        return await client.post(
            "/indexer/partition/target/file/copy/copy",
            data={"source_partition": "source", "source_file_id": "original", "metadata": '{"title": "Copy"}'},
        )


@pytest.mark.asyncio
async def test_copy_missing_source_returns_404_without_creating_target(copy_app):
    app, document_repo, dispatcher, partitions = copy_app
    document_repo.file_exists_in_partition.return_value = False

    response = await _copy(app)

    assert response.status_code == 404
    assert response.json()["detail"] == "[DOCUMENT_NOT_FOUND]: File 'original' not found in partition 'source'."
    document_repo.file_exists_in_partition.assert_awaited_once_with(file_id="original", partition="source")
    partitions.create_partition.assert_not_awaited()
    partitions.pin_embedder_for_write.assert_not_awaited()
    dispatcher.copy_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_copy_existing_source_still_creates_target_and_returns_201(copy_app):
    app, document_repo, dispatcher, partitions = copy_app

    response = await _copy(app)

    assert response.status_code == 201
    assert response.json() == {"message": "File copied successfully."}
    document_repo.file_exists_in_partition.assert_awaited_once_with(file_id="original", partition="source")
    assert partitions.create_partition.await_args.args == ("target",)
    partitions.pin_embedder_for_write.assert_awaited_once_with("target")
    dispatcher.copy_file.assert_awaited_once_with(
        "original",
        {"title": "Copy", "file_id": "copy", "partition": "target", "content_sha256": None},
        "source",
        {"id": 7, "is_admin": False},
    )


@pytest.mark.asyncio
async def test_copy_source_lookup_failure_is_not_reported_as_missing(copy_app):
    app, document_repo, dispatcher, partitions = copy_app
    document_repo.file_exists_in_partition.side_effect = StorageError("catalog unavailable")

    response = await _copy(app)

    assert response.status_code == 500
    assert response.json()["detail"].startswith("[STORAGE_ERROR]:")
    partitions.create_partition.assert_not_awaited()
    dispatcher.copy_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_copy_checks_source_access_before_looking_up_file(copy_app):
    app, document_repo, dispatcher, partitions = copy_app

    async def no_memberships():
        return []

    app.dependency_overrides[current_user_partitions] = no_memberships
    partitions.partition_exists.return_value = True

    response = await _copy(app)

    assert response.status_code == 403
    document_repo.file_exists_in_partition.assert_not_awaited()
    partitions.create_partition.assert_not_awaited()
    dispatcher.copy_file.assert_not_awaited()
