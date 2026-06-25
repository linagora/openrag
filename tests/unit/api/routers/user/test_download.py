"""Tests for the authorized source-file download route under /static.

Covers the partition-membership check and DATA_DIR confinement.
"""

from __future__ import annotations

from api.dependencies.auth import current_user_or_admin_partitions_list
from api.routers.user.download import router as download_router
from di.providers import get_conversion_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeConversionService:
    def __init__(self, chunk):
        self.chunk = chunk
        self.calls: list[str] = []

    async def get_chunk(self, chunk_id):
        self.calls.append(chunk_id)
        return self.chunk


def _client(chunk, partitions):
    app = FastAPI()
    app.include_router(download_router)
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: partitions

    app.dependency_overrides[get_conversion_service] = lambda: _FakeConversionService(chunk)
    return TestClient(app)


def _client_with_service(chunk, partitions):
    app = FastAPI()
    service = _FakeConversionService(chunk)
    app.include_router(download_router)
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: partitions
    app.dependency_overrides[get_conversion_service] = lambda: service
    return TestClient(app), service


def test_download_404_when_chunk_missing():
    client = _client(None, ["p1"])
    assert client.get("/static/123").status_code == 404


def test_download_403_when_user_lacks_partition_access():
    chunk = {"metadata": {"partition": "other-tenant", "source": "/data/secret.pdf"}}
    client = _client(chunk, ["p1"])
    r = client.get("/static/123")
    assert r.status_code == 403


def test_download_404_when_source_path_escapes_data_dir():
    # Partition access is granted, but the source path resolves outside DATA_DIR
    # (path-traversal attempt) → 404, never served.
    chunk = {"metadata": {"partition": "p1", "source": "/etc/passwd"}}
    client = _client(chunk, ["p1"])
    assert client.get("/static/123").status_code == 404


def test_download_404_when_source_missing():
    chunk = {"metadata": {"partition": "p1"}}
    client = _client(chunk, ["p1"])
    assert client.get("/static/123").status_code == 404


def test_download_rejects_invalid_extract_id_before_storage_lookup():
    client, service = _client_with_service(None, ["p1"])

    response = client.get("/static/bad%20id")

    assert response.status_code == 400
    assert service.calls == []
