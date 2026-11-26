"""Tests for the chunk-by-id route under /extract."""

from __future__ import annotations

import pytest
from api.dependencies.auth import current_user_or_admin_partitions_list
from api.routers.user.extract import router as extract_router
from di.providers import get_conversion_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeConversionService:
    def __init__(self, chunk):
        self.chunk = chunk

    async def get_chunk(self, chunk_id):
        return self.chunk


def _get(chunk, partitions):
    app = FastAPI()
    app.include_router(extract_router, prefix="/extract")
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: partitions
    app.dependency_overrides[get_conversion_service] = lambda: _FakeConversionService(chunk)
    return TestClient(app).get("/extract/123")


@pytest.mark.parametrize("partitions", [["p1"], ["all"]])
def test_extract_returns_a_chunk_of_a_readable_partition(partitions):
    # ["all"] is what a super admin (SUPER_ADMIN_MODE) gets.
    chunk = {"page_content": "text", "metadata": {"partition": "p1"}}
    r = _get(chunk, partitions)
    assert r.status_code == 200
    assert r.json() == chunk


def test_extract_in_another_partition_answers_like_a_missing_chunk():
    chunk = {"page_content": "text", "metadata": {"partition": "other-tenant"}}
    denied = _get(chunk, ["p1"])
    missing = _get(None, ["p1"])
    assert denied.status_code == 404
    assert denied.json() == missing.json()
