"""Unit tests for :class:`UserService` (Phase 8A.2)."""

from __future__ import annotations

import pytest
from core.models.user import User, UserCreate, UserUpdate
from core.utils.exceptions import UserNotFoundError, ValidationError
from services.orchestrators.user_service import UserService


class FakeUserRepo:
    def __init__(self, existing: set[int] | None = None):
        self._existing = existing if existing is not None else set()
        self.created: list[dict] = []
        self.deleted: list[int] = []
        self.regenerated: list[int] = []
        self.regen_results: dict[int, dict] = {}
        self.updated: list[tuple[int, dict]] = []
        self._users: dict[int, User] = {}

    async def user_exists(self, user_id: int) -> bool:
        return user_id in self._existing

    async def create_legacy_user(self, *, display_name, external_user_id, email, is_admin, file_quota):
        rec = {
            "id": 42,
            "display_name": display_name,
            "external_user_id": external_user_id,
            "email": email,
            "token": "or-deadbeef",
            "is_admin": is_admin,
            "file_quota": file_quota,
            "file_count": 0,
        }
        self.created.append(rec)
        return rec

    async def list_users_dict(self):
        return [{"id": 1, "display_name": "Admin"}]

    async def get_user_dict_by_id(self, user_id: int):
        return {"id": user_id, "display_name": "U"}

    async def delete_user(self, user_id: int) -> bool:
        self.deleted.append(user_id)
        return True

    async def regenerate_user_token(self, user_id: int):
        self.regenerated.append(user_id)
        return self.regen_results.get(user_id)

    async def update_user(self, user_id: int, **fields):
        self.updated.append((user_id, fields))
        return self._users.get(user_id)


class FakePartitionService:
    def __init__(self):
        self.deleted: list[str] = []

    async def delete_partition(self, partition: str) -> None:
        self.deleted.append(partition)


class FakeMembershipRepo:
    def __init__(self, owned: dict[int, list[dict]] | None = None):
        self._owned = owned or {}

    async def list_user_partitions_dict(self, user_id: int) -> list[dict]:
        return self._owned.get(user_id, [])


class FakeJobService:
    def __init__(self, pending: int = 0):
        self._pending = pending
        self.calls: list[int | None] = []

    async def get_user_pending_task_count(self, user_id: int | None) -> int:
        self.calls.append(user_id)
        return self._pending


def _svc(
    repo: FakeUserRepo,
    *,
    default_quota: int = 10,
    partition_service: FakePartitionService | None = None,
    membership_repo: FakeMembershipRepo | None = None,
    job_service: FakeJobService | None = None,
) -> UserService:
    return UserService(
        user_repo=repo,
        auth_service=object(),
        default_file_quota=default_quota,
        partition_service=partition_service or FakePartitionService(),
        membership_repo=membership_repo or FakeMembershipRepo(),
        job_service=job_service or FakeJobService(),
    )


# --------------------------------------------------------------------------- #
# create_user
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_user_passes_through_explicit_quota():
    repo = FakeUserRepo()
    svc = _svc(repo, default_quota=10)
    out = await svc.create_user(UserCreate(display_name="Bob", file_quota=3))
    assert out["token"] == "or-deadbeef"
    assert repo.created[0]["file_quota"] == 3


@pytest.mark.asyncio
async def test_create_user_applies_default_quota_when_none_and_default_positive():
    repo = FakeUserRepo()
    svc = _svc(repo, default_quota=7)
    await svc.create_user(UserCreate(display_name="Bob", file_quota=None))
    assert repo.created[0]["file_quota"] == 7


@pytest.mark.asyncio
async def test_create_user_no_default_when_default_not_positive():
    repo = FakeUserRepo()
    svc = _svc(repo, default_quota=-1)
    await svc.create_user(UserCreate(display_name="Bob", file_quota=None))
    assert repo.created[0]["file_quota"] is None


@pytest.mark.asyncio
async def test_create_user_rejects_bad_email():
    svc = _svc(FakeUserRepo())
    with pytest.raises(ValidationError) as ei:
        await svc.create_user(UserCreate(display_name="Bob", email="not-an-email"))
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_create_user_rejects_overlong_display_name():
    svc = _svc(FakeUserRepo())
    with pytest.raises(ValidationError):
        await svc.create_user(UserCreate(display_name="x" * 256))


# --------------------------------------------------------------------------- #
# read / delete / regenerate / update
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_users_delegates():
    assert await _svc(FakeUserRepo()).list_users() == [{"id": 1, "display_name": "Admin"}]


@pytest.mark.asyncio
async def test_get_user_missing_raises_404():
    svc = _svc(FakeUserRepo(existing=set()))
    with pytest.raises(UserNotFoundError) as ei:
        await svc.get_user(9)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_user_existing_returns_dict():
    svc = _svc(FakeUserRepo(existing={9}))
    assert await svc.get_user(9) == {"id": 9, "display_name": "U"}


@pytest.mark.asyncio
async def test_delete_user_missing_raises_and_no_repo_call():
    repo = FakeUserRepo(existing=set())
    with pytest.raises(UserNotFoundError):
        await _svc(repo).delete_user(5)
    assert repo.deleted == []


@pytest.mark.asyncio
async def test_delete_user_existing_no_owned_partitions():
    repo = FakeUserRepo(existing={5})
    ps = FakePartitionService()
    await _svc(repo, partition_service=ps).delete_user(5)
    assert repo.deleted == [5]
    assert ps.deleted == []


@pytest.mark.asyncio
async def test_delete_user_cascades_owned_partitions_first():
    repo = FakeUserRepo(existing={5})
    ps = FakePartitionService()
    mem = FakeMembershipRepo(
        {
            5: [
                {"partition": "p_owned", "role": "owner"},
                {"partition": "p_viewer", "role": "viewer"},  # not cascaded
            ]
        }
    )
    await _svc(repo, partition_service=ps, membership_repo=mem).delete_user(5)
    assert ps.deleted == ["p_owned"]  # only owner-role partitions
    assert repo.deleted == [5]


@pytest.mark.asyncio
async def test_regenerate_token_missing_user_404():
    repo = FakeUserRepo(existing=set())
    with pytest.raises(UserNotFoundError):
        await _svc(repo).regenerate_token(3)


@pytest.mark.asyncio
async def test_regenerate_token_repo_returns_none_404():
    repo = FakeUserRepo(existing={3})  # exists but repo regen returns None
    with pytest.raises(UserNotFoundError):
        await _svc(repo).regenerate_token(3)


@pytest.mark.asyncio
async def test_regenerate_token_success():
    repo = FakeUserRepo(existing={3})
    repo.regen_results[3] = {"id": 3, "token": "or-new"}
    out = await _svc(repo).regenerate_token(3)
    assert out == {"id": 3, "token": "or-new"}
    assert repo.regenerated == [3]


@pytest.mark.asyncio
async def test_update_user_missing_404():
    repo = FakeUserRepo(existing=set())
    with pytest.raises(UserNotFoundError):
        await _svc(repo).update_user(2, UserUpdate(display_name="X"))


@pytest.mark.asyncio
async def test_update_user_returns_legacy_dict_shape():
    repo = FakeUserRepo(existing={2})
    repo._users[2] = User(id=2, display_name="New", email="a@b.io", is_admin=True, file_quota=5, file_count=4)
    out = await _svc(repo).update_user(2, UserUpdate(display_name="New"))
    assert set(out) == {
        "id",
        "display_name",
        "external_user_id",
        "email",
        "is_admin",
        "created_at",
        "file_quota",
        "file_count",
    }
    assert out["id"] == 2 and out["display_name"] == "New" and out["file_count"] == 4
    assert isinstance(out["created_at"], str)  # iso-formatted


@pytest.mark.asyncio
async def test_update_user_validates_email():
    repo = FakeUserRepo(existing={2})
    with pytest.raises(ValidationError):
        await _svc(repo).update_user(2, UserUpdate(email="bogus"))


# --------------------------------------------------------------------------- #
# get_current_user_info — quota-usage block (8F: moved out of the router)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_current_user_info_specific_quota_and_pending():
    job = FakeJobService(pending=3)
    svc = _svc(FakeUserRepo(), default_quota=10, job_service=job)

    out = await svc.get_current_user_info({"id": 7, "is_admin": False, "file_quota": 5, "file_count": 4})

    assert out["file_count"] == 4
    assert out["pending_files"] == 3
    assert out["total_files"] == 7
    assert out["file_quota"] == 5
    assert out["id"] == 7  # original fields preserved
    assert job.calls == [7]


@pytest.mark.asyncio
async def test_current_user_info_admin_is_unlimited():
    svc = _svc(FakeUserRepo(), default_quota=10, job_service=FakeJobService(pending=1))
    out = await svc.get_current_user_info({"id": 1, "is_admin": True, "file_count": 2})
    assert out["file_quota"] == -1
    assert out["total_files"] == 3


@pytest.mark.asyncio
async def test_current_user_info_none_quota_falls_back_to_default():
    svc = _svc(FakeUserRepo(), default_quota=8, job_service=FakeJobService())
    out = await svc.get_current_user_info({"id": 2, "is_admin": False, "file_count": 0})
    assert out["file_quota"] == 8


@pytest.mark.asyncio
async def test_current_user_info_negative_default_is_unlimited():
    svc = _svc(FakeUserRepo(), default_quota=-1, job_service=FakeJobService())
    out = await svc.get_current_user_info({"id": 2, "is_admin": False, "file_quota": 3, "file_count": 0})
    assert out["file_quota"] == -1
