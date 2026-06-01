from types import SimpleNamespace

import pytest
from api.dependencies.auth import check_user_file_quota, ensure_partition_role, require_task_owner
from core.utils.exceptions import AuthError
from fastapi import HTTPException


class FakeAuthService:
    @classmethod
    def check_partition_access(
        cls,
        *,
        user,
        partition: str,
        user_partitions: list[dict],
        required_role: str,
        super_admin_mode: bool = False,
    ) -> bool:
        return True

    @classmethod
    def validate_file_quota(
        cls,
        user,
        *,
        pending_task_count: int,
        default_quota: int,
    ) -> None:
        return None


class EnforcingAuthService(FakeAuthService):
    @classmethod
    def validate_file_quota(
        cls,
        user,
        *,
        pending_task_count: int,
        default_quota: int,
    ) -> None:
        quota = user.get("file_quota")
        if quota is None:
            quota = default_quota
        if user.get("file_count", 0) + pending_task_count >= quota:
            raise AuthError("File quota exceeded")


class DenyingAuthService(FakeAuthService):
    @classmethod
    def check_partition_access(
        cls,
        *,
        user,
        partition: str,
        user_partitions: list[dict],
        required_role: str,
        super_admin_mode: bool = False,
    ) -> bool:
        raise AuthError(f"{required_role.capitalize()} role required for partition '{partition}'")


class FakePartitionService:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.checked: list[str] = []

    async def partition_exists(self, partition: str) -> bool:
        self.checked.append(partition)
        return partition in self.existing


class FakeJobService:
    def __init__(self, *, details=None, pending_count=0) -> None:
        self.details = details
        self.pending_count = pending_count
        self.detail_checks: list[str] = []
        self.pending_checks: list[int | None] = []

    async def get_task_details(self, task_id: str):
        self.detail_checks.append(task_id)
        return self.details

    async def get_user_pending_task_count(self, user_id: int | None) -> int:
        self.pending_checks.append(user_id)
        return self.pending_count


def _config(default_file_quota: int):
    return SimpleNamespace(rdb=SimpleNamespace(default_file_quota=default_file_quota))


@pytest.mark.asyncio
async def test_ensure_partition_role_allows_unknown_partition_without_membership():
    partition_service = FakePartitionService(existing=set())

    result = await ensure_partition_role(
        partition="new-partition",
        user={"id": 1},
        user_partitions=[],
        required_role="editor",
        auth_service=FakeAuthService,
        partition_service=partition_service,
    )

    assert result is True
    assert partition_service.checked == ["new-partition"]


@pytest.mark.asyncio
async def test_ensure_partition_role_forbids_existing_partition_without_membership():
    partition_service = FakePartitionService(existing={"existing"})

    with pytest.raises(HTTPException) as exc:
        await ensure_partition_role(
            partition="existing",
            user={"id": 1},
            user_partitions=[],
            required_role="viewer",
            auth_service=FakeAuthService,
            partition_service=partition_service,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Access to partition 'existing' forbidden"


@pytest.mark.asyncio
async def test_ensure_partition_role_delegates_membership_role_check_to_auth_service():
    partition_service = FakePartitionService(existing={"p"})

    with pytest.raises(HTTPException) as exc:
        await ensure_partition_role(
            partition="p",
            user={"id": 1},
            user_partitions=[{"partition": "p", "role": "viewer"}],
            required_role="editor",
            auth_service=DenyingAuthService,
            partition_service=partition_service,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Editor role required for partition 'p'"
    assert partition_service.checked == []


@pytest.mark.asyncio
async def test_require_task_owner_reads_task_details_through_job_service():
    job_service = FakeJobService(details={"user_id": 7, "filename": "a.pdf"})

    details = await require_task_owner(
        task_id="task-1",
        user={"id": 7},
        job_service=job_service,
    )

    assert details == {"user_id": 7, "filename": "a.pdf"}
    assert job_service.detail_checks == ["task-1"]


@pytest.mark.asyncio
async def test_check_user_file_quota_reads_pending_count_through_job_service():
    job_service = FakeJobService(pending_count=2)

    user = await check_user_file_quota(
        user={"id": 7, "file_count": 1, "file_quota": 5},
        auth_service=FakeAuthService,
        job_service=job_service,
        config=_config(default_file_quota=10),
    )

    assert user["id"] == 7
    assert job_service.pending_checks == [7]


@pytest.mark.asyncio
async def test_check_user_file_quota_default_zero_enforces_zero():
    allowed_job_service = FakeJobService(pending_count=0)
    user = await check_user_file_quota(
        user={"id": 7, "file_count": 0, "file_quota": 1},
        auth_service=EnforcingAuthService,
        job_service=allowed_job_service,
        config=_config(default_file_quota=0),
    )
    assert user["id"] == 7
    assert allowed_job_service.pending_checks == [7]

    denied_job_service = FakeJobService(pending_count=0)
    with pytest.raises(HTTPException) as exc:
        await check_user_file_quota(
            user={"id": 8, "file_count": 1, "file_quota": None},
            auth_service=EnforcingAuthService,
            job_service=denied_job_service,
            config=_config(default_file_quota=0),
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "File quota exceeded"
    assert denied_job_service.pending_checks == [8]
