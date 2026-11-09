"""Router tests for ``DELETE /users/{id}`` on operator-managed accounts (#1153)."""

from __future__ import annotations

from api.dependencies.auth import require_admin
from api.error_handlers import register_error_handlers
from api.routers.admin import users
from core.models.user import User
from di.providers import get_user_service
from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.orchestrators.user_service import UserService


class FakeUserRepo:
    def __init__(self, user: User) -> None:
        self._user = user
        self.deleted: list[int] = []

    async def user_exists(self, user_id: int) -> bool:
        return user_id == self._user.id

    async def get_user(self, user_id: int) -> User | None:
        return self._user if user_id == self._user.id else None

    async def delete_user(self, user_id: int) -> bool:
        self.deleted.append(user_id)
        return True


class FakePartitionService:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_partition(self, partition: str) -> None:
        self.deleted.append(partition)


class FakeMembershipRepo:
    async def list_user_partitions_dict(self, user_id: int) -> list[dict]:
        return [{"partition": "p_owned", "role": "owner"}]


def _client(repo: FakeUserRepo, partitions: FakePartitionService, seed_user_ids: frozenset[str]) -> TestClient:
    service = UserService(
        user_repo=repo,
        auth_service=object(),
        default_file_quota=0,
        partition_service=partitions,
        membership_repo=FakeMembershipRepo(),
        job_service=object(),
        seed_user_ids=seed_user_ids,
    )
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(users.router, prefix="/users")
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "is_admin": True}
    app.dependency_overrides[get_user_service] = lambda: service
    return TestClient(app)


def test_delete_of_a_listed_managed_account_is_a_409_and_deletes_nothing():
    repo = FakeUserRepo(User(id=2, display_name="svc", external_user_id="svc-a", managed_by_config=True))
    partitions = FakePartitionService()

    response = _client(repo, partitions, frozenset({"svc-a"})).delete("/users/2")

    assert response.status_code == 409
    assert response.json()["detail"].startswith("[CONFLICT]: ")
    assert "auth.seed_users" in response.json()["detail"]
    assert partitions.deleted == []
    assert repo.deleted == []


def test_delete_of_a_managed_account_no_longer_listed_is_a_204():
    repo = FakeUserRepo(User(id=2, display_name="svc", external_user_id="svc-a", managed_by_config=True))
    partitions = FakePartitionService()

    response = _client(repo, partitions, frozenset()).delete("/users/2")

    assert response.status_code == 204
    assert partitions.deleted == ["p_owned"]
    assert repo.deleted == [2]
