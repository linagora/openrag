from types import SimpleNamespace

from api.dependencies.auth import require_partition_viewer
from api.dependencies.retrieval_diagnostics import get_retrieval_diagnostics_guard
from api.error_handlers import register_error_handlers
from api.routers.user.search import router as search_router
from di.providers import get_config, get_query_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _AllowDiagnostics:
    async def authorize(self, _user):
        return None


def _config(max_context_tokens=4096):
    models = SimpleNamespace(
        llm={},
        llm_context_size=lambda _name: max_context_tokens,
        llm_output_tokens=lambda _name: 1,
    )
    return SimpleNamespace(
        models=models,
        partitions={},
        llm_context=SimpleNamespace(max_llm_context_size=max_context_tokens, max_output_tokens=1),
        rag=SimpleNamespace(max_contextualized_query_len=2),
    )


def _client(*, max_context_tokens=4096, user=None):
    class _QueryService:
        def __init__(self):
            self.calls = []

        async def diagnose_retrieval(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "schema_version": 1,
                "request_id": "diagnostic-request",
                "original_query": "What changed?",
                "stages": [],
                "query_traces": [],
                "timings": {},
                "errors": [],
            }

    service = _QueryService()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    viewer = user or {"id": 1, "is_admin": True}
    app.dependency_overrides[require_partition_viewer] = lambda: viewer
    if viewer["is_admin"]:
        app.dependency_overrides[get_retrieval_diagnostics_guard] = lambda: _AllowDiagnostics()
    app.dependency_overrides[get_query_service] = lambda: service
    app.dependency_overrides[get_config] = lambda: _config(max_context_tokens)
    return TestClient(app), service


def test_endpoint_runs_typed_retrieval_without_answer_generation():
    client, service = _client()

    response = client.post(
        "/search/partition/legal/diagnostics",
        headers={"X-Request-ID": "request-from-header"},
        json={
            "messages": [{"role": "user", "content": "What changed?"}],
            "query_mode": "compare",
            "top_k": 50,
            "similarity_threshold": 0.25,
            "disable_reranker": True,
            "disable_expansion": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["schema_status"] == "experimental"
    assert response.json()["retrieval_trace"]["request_id"] == "diagnostic-request"
    assert service.calls == [
        {
            "partitions": ["legal"],
            "messages": [{"role": "user", "content": "What changed?"}],
            "query_mode": "compare",
            "top_k": 50,
            "similarity_threshold": 0.25,
            "disable_reranker": True,
            "disable_expansion": True,
            "request_id": "request-from-header",
        }
    ]


def test_endpoint_rejects_unbounded_and_unknown_input():
    client, service = _client()

    response = client.post(
        "/search/partition/legal/diagnostics",
        json={
            "messages": [{"role": "user", "content": "What changed?"}],
            "top_k": 1001,
            "include_document_content": True,
        },
    )

    assert response.status_code == 422
    assert service.calls == []


def test_endpoint_rejects_input_over_context_budget():
    client, service = _client(max_context_tokens=8)

    response = client.post(
        "/search/partition/legal/diagnostics",
        json={"messages": [{"role": "user", "content": "one two three four five six seven eight"}]},
    )

    assert response.status_code == 413
    assert service.calls == []


def test_endpoint_rejects_excessive_message_fanout():
    client, service = _client()

    response = client.post(
        "/search/partition/legal/diagnostics",
        json={
            "messages": [
                {"role": "assistant" if index < 100 else "user", "content": f"message {index}"} for index in range(101)
            ]
        },
    )

    assert response.status_code == 422
    assert service.calls == []


def test_endpoint_requires_administrator_privileges():
    client, service = _client(user={"id": 7, "is_admin": False})

    response = client.post(
        "/search/partition/legal/diagnostics",
        json={"messages": [{"role": "user", "content": "What changed?"}]},
    )

    assert response.status_code == 403
    assert service.calls == []
