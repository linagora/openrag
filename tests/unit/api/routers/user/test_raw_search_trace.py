import json
from types import SimpleNamespace

from api.dependencies.auth import (
    current_user,
    current_user_or_admin_partitions_list,
    current_user_partitions,
    require_partition_viewer,
)
from api.dependencies.files import validate_file_id
from api.dependencies.retrieval_diagnostics import get_retrieval_diagnostics_guard
from api.error_handlers import register_error_handlers
from api.routers.user.search import router as search_router
from core.models.chunk import Chunk
from core.retrieval.trace import candidates_from_chunks, canonical_fingerprint
from di.providers import get_retrieval_service, get_workspace_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _AllowDiagnostics:
    async def authorize(self, _user):
        return None


class _Workspaces:
    def __init__(self, scope=None):
        self.scope = scope

    async def resolve_scope(self, _workspace, _partitions):
        return self.scope


class _Retrieval:
    def __init__(self):
        self.calls = []
        self.fail_fingerprint = False

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        chunk = Chunk(
            id="chunk-1",
            document_id="doc-1",
            text="private document body",
            metadata={"api_key": "secret-token"},
            partition="mine",
        )
        trace = kwargs.get("trace")
        if trace is not None:
            trace.record_stage("final", status="complete", candidates=candidates_from_chunks([chunk]))
        return [chunk]

    def search_configuration_fingerprint(self, partitions, effective_options):
        if self.fail_fingerprint:
            raise RuntimeError("configuration lookup failed")
        return canonical_fingerprint(
            {
                "partitions": list(partitions),
                "request": dict(effective_options),
            }
        )


def _client(*, retrieval=None, user=None, workspaces=None):
    retrieval = retrieval or _Retrieval()
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/extract/{extract_id}", name="get_extract")
    async def get_extract(extract_id: str):
        return {"id": extract_id}

    app.include_router(search_router, prefix="/search")
    viewer = user or {"id": 1, "is_admin": True}
    app.dependency_overrides[require_partition_viewer] = lambda: viewer
    app.dependency_overrides[current_user] = lambda: viewer
    app.dependency_overrides[current_user_partitions] = lambda: [{"partition": "mine", "role": "viewer"}]
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: ["mine"]
    app.dependency_overrides[get_retrieval_service] = lambda: retrieval
    app.dependency_overrides[get_workspace_service] = lambda: workspaces or _Workspaces()
    app.dependency_overrides[get_retrieval_diagnostics_guard] = lambda: _AllowDiagnostics()
    return TestClient(app), retrieval


def test_untraced_search_preserves_existing_response_and_skips_collector():
    client, retrieval = _client()

    payload = client.get("/search/partition/mine", params={"text": "q"}).json()

    assert set(payload) == {"documents"}
    assert retrieval.calls[0].get("trace") is None


def test_traced_search_keeps_documents_and_excludes_sensitive_content():
    client, retrieval = _client()
    plain = client.get("/search/partition/mine", params={"text": "q"}).json()["documents"]

    response = client.get(
        "/search/partition/mine",
        params={"text": "q", "include_retrieval_trace": True},
        headers={"X-Request-ID": "trace-request"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["documents"] == plain
    assert payload["retrieval_trace"]["request_id"] == "trace-request"
    assert retrieval.calls[-1]["trace"] is not None
    serialized = json.dumps(payload["retrieval_trace"])
    assert "private document body" not in serialized
    assert "secret-token" not in serialized


def test_trace_fingerprint_contains_effective_search_options():
    client, _retrieval = _client()

    response = client.get(
        "/search/partition/mine",
        params={
            "text": "q",
            "top_k": 17,
            "similarity_threshold": 0.35,
            "include_related": True,
            "include_ancestors": True,
            "related_limit": 8,
            "max_ancestor_depth": 4,
            "include_retrieval_trace": True,
        },
    )

    expected = canonical_fingerprint(
        {
            "partitions": ["mine"],
            "request": {
                "top_k": 17,
                "similarity_threshold": 0.35,
                "include_related": True,
                "include_ancestors": True,
                "related_limit": 8,
                "max_ancestor_depth": 4,
            },
        }
    )
    assert response.json()["retrieval_trace"]["configuration_fingerprint"] == expected


def test_trace_marks_failed_fingerprint_resolution_unavailable():
    retrieval = _Retrieval()
    retrieval.fail_fingerprint = True
    client, _retrieval = _client(retrieval=retrieval)

    response = client.get(
        "/search/partition/mine",
        params={"text": "q", "include_retrieval_trace": True},
    )

    assert response.status_code == 200
    assert response.json()["retrieval_trace"]["configuration_fingerprint"] == "unavailable"


def test_trace_requires_administrator_privileges():
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[require_partition_viewer] = lambda: {"id": 7, "is_admin": False}
    app.dependency_overrides[get_retrieval_service] = lambda: _Retrieval()
    app.dependency_overrides[get_workspace_service] = lambda: _Workspaces()

    response = TestClient(app).get(
        "/search/partition/mine",
        params={"text": "q", "include_retrieval_trace": True},
    )

    assert response.status_code == 403


def test_file_trace_labels_explicit_file_scope():
    client, retrieval = _client()
    client.app.dependency_overrides[validate_file_id] = lambda: "abc123"

    response = client.get(
        "/search/partition/mine/file/abc123",
        params={"text": "q", "include_retrieval_trace": True},
    )

    assert response.status_code == 200
    assert retrieval.calls[-1]["filter_params"] == {
        "file_id": "abc123",
        "_trace_file_scope_kind": "file",
    }


def test_workspace_trace_labels_explicit_workspace_scope():
    scope = SimpleNamespace(partition="mine", file_ids=["fa", "fb"])
    client, retrieval = _client(workspaces=_Workspaces(scope))

    response = client.get(
        "/search/partition/mine",
        params={"text": "q", "workspace": "w1", "include_retrieval_trace": True},
    )

    assert response.status_code == 200
    assert retrieval.calls[-1]["filter_params"] == {
        "file_id": ["fa", "fb"],
        "_trace_file_scope_kind": "workspace",
    }


def test_raw_search_rejects_top_k_above_resource_limit():
    client, _retrieval = _client()

    response = client.get("/search/partition/mine", params={"text": "q", "top_k": 1001})

    assert response.status_code == 422
