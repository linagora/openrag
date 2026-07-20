import asyncio
from types import SimpleNamespace

import pytest
from api.dependencies.auth import (
    check_user_file_quota,
    commit_quota_reservation,
    ensure_partition_role,
    require_partitions_viewer,
    require_task_owner,
)
from core.utils.exceptions import AuthError, OpenRAGError
from fastapi import HTTPException
from services.orchestrators.auth_service import AuthService


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


@pytest.mark.parametrize("role", ["viewer", "owner"])
@pytest.mark.asyncio
async def test_ensure_partition_role_404s_on_missing_partition_for_non_editor(role):
    # A non-member must not pass a viewer/owner check by naming an unknown
    # partition. Only `editor` (create-on-write) may proceed on a missing
    # partition.
    partition_service = FakePartitionService(existing=set())

    with pytest.raises(HTTPException) as exc:
        await ensure_partition_role(
            partition="ghost",
            user={"id": 1},
            user_partitions=[],
            required_role=role,
            auth_service=FakeAuthService,
            partition_service=partition_service,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Partition 'ghost' not found"


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
async def test_require_partitions_viewer_fails_closed_for_all_scope_without_memberships():
    with pytest.raises(HTTPException) as exc:
        await require_partitions_viewer(
            partitions=["all"],
            user={"id": 1, "is_admin": False},
            user_partitions=[],
            auth_service=FakeAuthService,
            partition_service=FakePartitionService(existing=set()),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "No accessible partitions"


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
        auth_service=AuthService,
    )

    assert details == {"user_id": 7, "filename": "a.pdf"}
    assert job_service.detail_checks == ["task-1"]


@pytest.mark.asyncio
async def test_require_task_owner_allows_admin_for_another_users_task():
    # Admins may open any task (parity with the admin jobs list) — no 403 dead-end.
    job_service = FakeJobService(details={"user_id": 2, "filename": "b.pdf"})

    details = await require_task_owner(
        task_id="task-2",
        user={"id": 1, "is_admin": True},
        job_service=job_service,
        auth_service=AuthService,
    )

    assert details == {"user_id": 2, "filename": "b.pdf"}


@pytest.mark.asyncio
async def test_require_task_owner_rejects_non_owner_non_admin():
    job_service = FakeJobService(details={"user_id": 2, "filename": "b.pdf"})

    with pytest.raises(HTTPException) as exc:
        await require_task_owner(
            task_id="task-2",
            user={"id": 1, "is_admin": False},
            job_service=job_service,
            auth_service=AuthService,
        )
    assert exc.value.status_code == 403


class RecordingAuthService(FakeAuthService):
    """Stands in for AuthService's reserve/release pair (#664)."""

    def __init__(self, *, grant: bool = True, raise_on_release: bool = False) -> None:
        self.grant = grant
        self.raise_on_release = raise_on_release
        self.reserved: list[tuple[int, int]] = []
        self.released: list[int] = []

    async def reserve_file_slot(self, user_id: int, *, default_quota: int) -> int:
        self.reserved.append((user_id, default_quota))
        if not self.grant:
            raise OpenRAGError(
                "File quota exceeded.",
                code="FILE_QUOTA_EXCEEDED",
                status_code=403,
            )
        return len(self.reserved)

    async def release_file_slot(self, user_id: int) -> None:
        self.released.append(user_id)


async def _drive_quota_dep(auth_service, user, default_quota: int, *, commit: bool, boom: Exception | None = None):
    """Run the dependency generator the way FastAPI's exit stack does."""
    gen = check_user_file_quota(
        user=user,
        auth_service=auth_service,
        config=_config(default_file_quota=default_quota),
    )
    reservation = await gen.__anext__()
    if commit:
        commit_quota_reservation(reservation)
    try:
        if boom is not None:
            # FastAPI throws the endpoint's exception back into the generator.
            await gen.athrow(boom)
        else:
            await gen.__anext__()
    except StopAsyncIteration:
        pass
    except type(boom) if boom is not None else ():
        pass
    return reservation


@pytest.mark.asyncio
async def test_check_user_file_quota_reserves_a_slot_atomically():
    auth_service = RecordingAuthService()

    reservation = await _drive_quota_dep(
        auth_service,
        {"id": 7, "file_count": 1, "file_quota": 5},
        default_quota=10,
        commit=True,
    )

    assert auth_service.reserved == [(7, 10)]
    assert reservation.user_id == 7
    assert reservation.committed is True


@pytest.mark.asyncio
async def test_check_user_file_quota_rejects_with_403_when_no_slot():
    """No row from the conditional UPDATE ⇒ the upload is refused."""
    auth_service = RecordingAuthService(grant=False)

    gen = check_user_file_quota(
        user={"id": 8, "file_count": 5, "file_quota": 5},
        auth_service=auth_service,
        config=_config(default_file_quota=10),
    )
    with pytest.raises(HTTPException) as exc:
        await gen.__anext__()

    assert exc.value.status_code == 403
    assert "quota" in exc.value.detail.lower()
    # A rejected reserve took nothing, so there is nothing to hand back.
    assert auth_service.released == []


@pytest.mark.asyncio
async def test_committed_reservation_is_not_released():
    """After dispatch the worker owns the slot; teardown must keep its hands off."""
    auth_service = RecordingAuthService()

    await _drive_quota_dep(auth_service, {"id": 7}, default_quota=5, commit=True)

    assert auth_service.released == []


@pytest.mark.asyncio
async def test_uncommitted_reservation_is_released_on_teardown():
    """The route returned early (e.g. 409 duplicate) — the slot goes back."""
    auth_service = RecordingAuthService()

    await _drive_quota_dep(auth_service, {"id": 7}, default_quota=5, commit=False)

    assert auth_service.released == [7]


@pytest.mark.asyncio
async def test_reservation_is_released_when_the_endpoint_raises():
    """FastAPI throws the endpoint error into the dependency; cleanup still runs."""
    auth_service = RecordingAuthService()

    await _drive_quota_dep(
        auth_service,
        {"id": 7},
        default_quota=5,
        commit=False,
        boom=HTTPException(status_code=409, detail="already exists"),
    )

    assert auth_service.released == [7]


@pytest.mark.asyncio
async def test_reservation_is_released_when_the_client_disconnects():
    """A cancelled request must not leak the slot it admitted."""
    auth_service = RecordingAuthService()

    await _drive_quota_dep(
        auth_service,
        {"id": 7},
        default_quota=5,
        commit=False,
        boom=asyncio.CancelledError(),
    )

    assert auth_service.released == [7]


@pytest.mark.asyncio
async def test_check_user_file_quota_no_op_without_a_user_id():
    """Nothing durable to charge (auth disabled) ⇒ no reserve, no release."""
    auth_service = RecordingAuthService()

    reservation = await _drive_quota_dep(auth_service, {}, default_quota=5, commit=False)

    assert reservation is None
    assert auth_service.reserved == []
    assert auth_service.released == []


@pytest.mark.asyncio
async def test_admin_reservation_still_counts_but_is_never_rejected():
    """Admins bypass the *limit*, not the counter — the SQL predicate decides."""
    auth_service = RecordingAuthService()

    await _drive_quota_dep(auth_service, {"id": 1, "is_admin": True}, default_quota=0, commit=True)

    assert auth_service.reserved == [(1, 0)]


def test_commit_quota_reservation_ignores_non_reservations():
    """Tests routinely override the dependency with a plain stub."""
    commit_quota_reservation(None)
    commit_quota_reservation({"id": 1})
