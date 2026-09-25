from api.dependencies.auth import require_partition_viewer
from api.dependencies.retrieval_diagnostics import get_retrieval_diagnostics_guard
from api.error_handlers import register_error_handlers
from api.routers.user.search import router as search_router
from di.providers import get_retrieval_snapshot_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _AllowDiagnostics:
    async def authorize(self, _user):
        return None


def test_snapshot_route_forwards_opt_in_document_ids():
    class _Snapshots:
        def __init__(self):
            self.calls = []

        async def snapshot(self, partition, *, include_document_ids=False):
            self.calls.append((partition, include_document_ids))
            return {"configuration": {}, "index": {"partition": partition}, "fingerprint": "fp"}

    snapshots = _Snapshots()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[require_partition_viewer] = lambda: {"id": 1, "is_admin": True}
    app.dependency_overrides[get_retrieval_snapshot_service] = lambda: snapshots
    app.dependency_overrides[get_retrieval_diagnostics_guard] = lambda: _AllowDiagnostics()

    response = TestClient(app).get(
        "/search/partition/legal/snapshot",
        params={"include_document_ids": True},
    )

    assert response.status_code == 200
    assert response.json()["fingerprint"] == "fp"
    assert snapshots.calls == [("legal", True)]


def test_snapshot_requires_admin_privileges_even_without_document_ids():
    class _Snapshots:
        async def snapshot(self, partition, *, include_document_ids=False):
            return {"index": {"partition": partition}}

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[require_partition_viewer] = lambda: {"id": 7, "is_admin": False}
    app.dependency_overrides[get_retrieval_snapshot_service] = lambda: _Snapshots()

    response = TestClient(app).get("/search/partition/legal/snapshot")

    assert response.status_code == 403
