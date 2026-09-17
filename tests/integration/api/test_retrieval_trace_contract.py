"""Transport-level retrieval trace contract without live inference services."""

from __future__ import annotations

import json

from api.dependencies.auth import require_partition_viewer
from api.error_handlers import register_error_handlers
from api.routers.user.search import router as search_router
from core.models.chunk import Chunk
from core.retrieval.trace import candidates_from_chunks
from di.providers import get_retrieval_service, get_workspace_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Retrieval:
    async def search(self, **kwargs):
        chunk = Chunk(
            id="chunk-1",
            document_id="doc-1",
            text="private document body",
            metadata={"api_key": "secret-token"},
        )
        trace = kwargs.get("trace")
        if trace is not None:
            trace.record_stage("final", status="complete", candidates=candidates_from_chunks([chunk]))
        return [chunk]

    @staticmethod
    def configuration_fingerprint(_partitions):
        return "public-fingerprint"


def _client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/extract/{extract_id}", name="get_extract")
    async def get_extract(extract_id: str):
        return {"id": extract_id}

    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[require_partition_viewer] = lambda: {"id": 1, "is_admin": True}
    app.dependency_overrides[get_retrieval_service] = lambda: _Retrieval()
    app.dependency_overrides[get_workspace_service] = lambda: object()
    return TestClient(app)


def test_trace_contract_is_opt_in_and_contains_no_sensitive_payload():
    client = _client()

    normal = client.get("/search/partition/p", params={"text": "q"}).json()
    traced = client.get(
        "/search/partition/p",
        params={"text": "q", "include_retrieval_trace": True},
    ).json()

    assert set(normal) == {"documents"}
    assert traced["retrieval_trace"]["schema_version"] == 1
    serialized = json.dumps(traced["retrieval_trace"])
    assert "private document body" not in serialized
    assert "secret-token" not in serialized
