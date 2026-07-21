from types import SimpleNamespace

import pytest
from api.dependencies.auth import (
    check_user_file_quota,
    ensure_partition_role,
    require_partitions_viewer,
    require_task_owner,
)
from core.utils.exceptions import AuthError
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
async def test_check_user_file_quota_skips_pending_count_when_default_is_unlimited():
    """Skip queue I/O when the resolved quota is unlimited."""
    job_service = FakeJobService(pending_count=2)

    user = await check_user_file_quota(
        user={"id": 7, "file_count": 1, "file_quota": None},
        auth_service=FakeAuthService,
        job_service=job_service,
        config=_config(default_file_quota=-1),
    )

    assert user["id"] == 7
    assert job_service.pending_checks == []


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


# ---------------------------------------------------------------------------
# #725 — the role literal each wrapper passes must itself be asserted.
#
# `ensure_partition_role` is well covered, but every existing test of the thin
# wrappers replaces them via `dependency_overrides`, so the wrapper body — and
# the role string it hardcodes — never executes. Mutation testing showed five of
# six wrappers could be made allow-all with zero failures, and a one-word slip
# ("owner" -> "viewer") across three byte-identical wrappers would ship green.
#
# These call each wrapper DIRECTLY and spy on the role it forwards.
# ---------------------------------------------------------------------------


class RoleSpy:
    """Records the required_role each wrapper forwards to ensure_partition_role."""

    def __init__(self) -> None:
        self.roles: list[str] = []
        self.partitions: list[str] = []

    async def __call__(self, partition, user, user_partitions, required_role, **kwargs):
        self.partitions.append(partition)
        self.roles.append(required_role)
        return True


@pytest.fixture
def role_spy(monkeypatch):
    spy = RoleSpy()
    monkeypatch.setattr("api.dependencies.auth.ensure_partition_role", spy)
    return spy


@pytest.mark.parametrize(
    ("wrapper_name", "expected_role"),
    [
        ("require_partition_viewer", "viewer"),
        ("require_partition_editor", "editor"),
        ("require_partition_owner", "owner"),
    ],
)
@pytest.mark.asyncio
async def test_partition_wrappers_forward_their_own_role(role_spy, wrapper_name, expected_role):
    import api.dependencies.auth as auth_deps

    wrapper = getattr(auth_deps, wrapper_name)
    user = {"id": 1, "is_admin": False}

    returned = await wrapper(
        partition="p1",
        user=user,
        user_partitions=[],
        auth_service=FakeAuthService,
        partition_service=FakePartitionService(existing={"p1"}),
    )

    assert role_spy.roles == [expected_role], f"{wrapper_name} must require '{expected_role}'"
    assert role_spy.partitions == ["p1"]
    assert returned is user


@pytest.mark.asyncio
async def test_require_partitions_viewer_forwards_viewer_for_each_partition(role_spy):
    user = {"id": 1, "is_admin": False}

    await require_partitions_viewer(
        partitions=["a", "b"],
        user=user,
        user_partitions=[{"partition": "a"}, {"partition": "b"}],
        auth_service=FakeAuthService,
        partition_service=FakePartitionService(existing={"a", "b"}),
    )

    assert role_spy.roles == ["viewer", "viewer"]
    assert role_spy.partitions == ["a", "b"]


# --- require_admin / require_admin_or_self: previously 0 direct references ---


def test_require_admin_rejects_non_admin():
    from api.dependencies.auth import require_admin

    with pytest.raises(HTTPException) as exc:
        require_admin(user={"id": 2, "is_admin": False})
    assert exc.value.status_code == 403


def test_require_admin_rejects_missing_user():
    from api.dependencies.auth import require_admin

    with pytest.raises(HTTPException) as exc:
        require_admin(user=None)
    assert exc.value.status_code == 403


def test_require_admin_allows_admin():
    from api.dependencies.auth import require_admin

    user = {"id": 1, "is_admin": True}
    assert require_admin(user=user) is user


@pytest.mark.parametrize(
    ("user", "target_user_id", "allowed"),
    [
        ({"id": 5, "is_admin": True}, 9, True),  # admin acting on someone else
        ({"id": 5, "is_admin": False}, 5, True),  # self
        ({"id": 5, "is_admin": False}, 9, False),  # another user's account
        ({"id": 5, "is_admin": False}, None, False),  # no target resolved
    ],
)
def test_require_admin_or_self_matrix(user, target_user_id, allowed):
    """Guards token regeneration; had zero test references before #725."""
    from api.dependencies.auth import require_admin_or_self

    if allowed:
        assert require_admin_or_self(target_user_id=target_user_id, user=user) is user
    else:
        with pytest.raises(HTTPException) as exc:
            require_admin_or_self(target_user_id=target_user_id, user=user)
        assert exc.value.status_code == 403


def test_require_admin_or_self_rejects_missing_user():
    from api.dependencies.auth import require_admin_or_self

    with pytest.raises(HTTPException) as exc:
        require_admin_or_self(target_user_id=1, user=None)
    assert exc.value.status_code == 403


# --- SUPER_ADMIN_MODE: the deny side had no test at all ---


@pytest.mark.asyncio
async def test_admin_does_not_bypass_partition_check_when_super_admin_mode_off(monkeypatch):
    """With SUPER_ADMIN_MODE off, is_admin must NOT grant cross-partition access.

    Dropping the `SUPER_ADMIN_MODE and ...` conjunct (making it permanently on)
    previously failed no test.
    """
    monkeypatch.setattr("api.dependencies.auth.SUPER_ADMIN_MODE", False)

    with pytest.raises(HTTPException) as exc:
        await ensure_partition_role(
            "other-tenant",
            {"id": 1, "is_admin": True},
            [],  # no membership
            "viewer",
            auth_service=FakeAuthService,
            partition_service=FakePartitionService(existing={"other-tenant"}),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_bypasses_partition_check_when_super_admin_mode_on(monkeypatch):
    monkeypatch.setattr("api.dependencies.auth.SUPER_ADMIN_MODE", True)

    assert await ensure_partition_role(
        "other-tenant",
        {"id": 1, "is_admin": True},
        [],
        "viewer",
        auth_service=FakeAuthService,
        partition_service=FakePartitionService(existing={"other-tenant"}),
    )


# --- quota: the in-flight half (the #664 race) was unverified ---


@pytest.mark.asyncio
async def test_quota_denies_on_pending_tasks_alone():
    """Pending tasks alone must be able to exhaust the quota.

    Uses the real AuthService, not a fake: forcing `pending_task_count=0` in the
    dependency previously failed no test, because every enforcing case already
    passed pending_count=0. This is the in-flight half that #664 is about.
    """
    job_service = FakeJobService(pending_count=5)

    with pytest.raises(HTTPException) as exc:
        await check_user_file_quota(
            user={"id": 7, "is_admin": False, "file_count": 0, "file_quota": 5},
            auth_service=AuthService,
            job_service=job_service,
            config=_config(default_file_quota=10),
        )

    assert exc.value.status_code == 403
    assert job_service.pending_checks == [7]


@pytest.mark.asyncio
async def test_quota_allows_when_indexed_plus_pending_is_under_limit():
    job_service = FakeJobService(pending_count=1)

    user = await check_user_file_quota(
        user={"id": 7, "is_admin": False, "file_count": 1, "file_quota": 5},
        auth_service=AuthService,
        job_service=job_service,
        config=_config(default_file_quota=10),
    )
    assert user["id"] == 7
