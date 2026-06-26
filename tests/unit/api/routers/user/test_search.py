from api.dependencies.auth import (
    current_user,
    current_user_or_admin_partitions_list,
    current_user_partitions,
)
from api.routers.user.search import router as search_router
from di.providers import get_auth_service, get_partition_service, get_retrieval_service, get_workspace_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _AuthService:
    @staticmethod
    def check_partition_access(**_kwargs):
        return True


class _PartitionService:
    async def partition_exists(self, _partition):
        return False


class _RetrievalService:
    async def search(self, **_kwargs):
        raise AssertionError("search must not run without an authorized partition scope")


def _client(*, user_partitions):
    app = FastAPI()
    app.include_router(search_router, prefix="/search")
    app.dependency_overrides[current_user] = lambda: {"id": 7, "is_admin": False}
    app.dependency_overrides[current_user_partitions] = lambda: user_partitions
    app.dependency_overrides[current_user_or_admin_partitions_list] = lambda: [
        row["partition"] for row in user_partitions
    ]
    app.dependency_overrides[get_auth_service] = lambda: _AuthService
    app.dependency_overrides[get_partition_service] = lambda: _PartitionService()
    app.dependency_overrides[get_retrieval_service] = lambda: _RetrievalService()
    app.dependency_overrides[get_workspace_service] = lambda: object()
    return TestClient(app)


def test_search_default_all_fails_closed_when_user_has_no_partitions():
    response = _client(user_partitions=[]).get("/search", params={"text": "hello"})

    assert response.status_code == 403
    assert response.json()["detail"] == "No accessible partitions"
