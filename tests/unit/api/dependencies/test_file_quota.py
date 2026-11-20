"""``check_user_file_quota`` reserves a slot and owns it until handed off (#664)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from api.dependencies.auth import (
    FileSlotReservation,
    check_user_file_quota,
    file_slot_reservation_id,
    hand_off_file_slot,
)
from core.models.user import FileSlotGrant
from fastapi import HTTPException
from services.orchestrators.auth_service import AuthService


class _SlotRepo:
    def __init__(self, grant: FileSlotGrant = FileSlotGrant.RESERVED) -> None:
        self.grant = grant
        self.reserved: list[tuple[int, str, int]] = []
        self.released: list[str] = []

    async def try_reserve_file_slot(self, user_id, reservation_id, *, default_quota):
        self.reserved.append((user_id, reservation_id, default_quota))
        return self.grant

    async def release_file_slot(self, reservation_id):
        self.released.append(reservation_id)
        return True


def _auth(repo: _SlotRepo) -> AuthService:
    return AuthService(
        user_repo=repo,
        oidc_session_repo=object(),
        membership_repo=object(),
        oidc_client=None,
        config=SimpleNamespace(),
    )


def _config(default_file_quota: int = 10):
    return SimpleNamespace(rdb=SimpleNamespace(default_file_quota=default_file_quota))


_CAPPED = {"id": 7, "is_admin": False, "file_quota": 5, "file_count": 1}


def _gate(repo: _SlotRepo, user: dict = _CAPPED, default_file_quota: int = 10):
    return check_user_file_quota(user=user, auth_service=_auth(repo), config=_config(default_file_quota))


@pytest.mark.asyncio
async def test_a_capped_user_gets_a_reservation():
    repo = _SlotRepo()
    gate = _gate(repo)

    reservation = await anext(gate)

    assert isinstance(reservation, FileSlotReservation)
    assert repo.reserved == [(7, reservation.id, 10)]
    assert file_slot_reservation_id(reservation) == reservation.id
    await gate.aclose()


@pytest.mark.asyncio
async def test_a_route_that_fails_before_hand_off_gives_the_slot_back():
    """409 duplicate, rejected upload, unknown workspace, dispatch error: all reach here."""
    repo = _SlotRepo()
    gate = _gate(repo)
    reservation = await anext(gate)

    with pytest.raises(HTTPException):
        await gate.athrow(HTTPException(status_code=409, detail="exists"))

    assert repo.released == [reservation.id]


@pytest.mark.asyncio
async def test_a_cancelled_request_gives_the_slot_back():
    """A client disconnect cancels the route; ``CancelledError`` is a BaseException."""
    repo = _SlotRepo()
    gate = _gate(repo)
    reservation = await anext(gate)

    with pytest.raises(asyncio.CancelledError):
        await gate.athrow(asyncio.CancelledError())

    assert repo.released == [reservation.id]


@pytest.mark.asyncio
async def test_a_route_that_returns_without_hand_off_gives_the_slot_back():
    """The copy route never hands off: its release after a consumed slot is a no-op in the repo."""
    repo = _SlotRepo()
    gate = _gate(repo)
    reservation = await anext(gate)

    with pytest.raises(StopAsyncIteration):
        await anext(gate)

    assert repo.released == [reservation.id]


@pytest.mark.asyncio
async def test_a_handed_off_slot_is_left_to_the_task():
    repo = _SlotRepo()
    gate = _gate(repo)
    reservation = await anext(gate)

    hand_off_file_slot(reservation)
    with pytest.raises(StopAsyncIteration):
        await anext(gate)

    assert repo.released == []


@pytest.mark.asyncio
async def test_a_full_quota_is_refused_with_403_and_holds_nothing():
    repo = _SlotRepo(FileSlotGrant.DENIED)

    with pytest.raises(HTTPException) as exc:
        await anext(_gate(repo))

    assert exc.value.status_code == 403
    assert repo.released == []


@pytest.mark.parametrize(
    ("user", "default_file_quota"),
    [
        ({"id": 1, "is_admin": True, "file_quota": 0}, 0),
        ({"id": 7, "is_admin": False, "file_quota": -1}, 10),
        ({"id": 7, "is_admin": False, "file_quota": None}, -1),
        ({"is_admin": False, "file_quota": 5}, 10),
    ],
    ids=["admin", "unlimited-user", "unlimited-default", "no-user-id"],
)
@pytest.mark.asyncio
async def test_nothing_is_reserved_for_an_unlimited_user(user, default_file_quota):
    repo = _SlotRepo()
    gate = _gate(repo, user=user, default_file_quota=default_file_quota)

    assert await anext(gate) is None
    with pytest.raises(StopAsyncIteration):
        await anext(gate)

    assert repo.reserved == []
    assert repo.released == []


@pytest.mark.asyncio
async def test_a_quota_lifted_since_authentication_reserves_nothing():
    """The request-state user said capped; the database says unlimited now."""
    repo = _SlotRepo(FileSlotGrant.UNLIMITED)
    gate = _gate(repo)

    assert await anext(gate) is None
    with pytest.raises(StopAsyncIteration):
        await anext(gate)

    assert len(repo.reserved) == 1
    assert repo.released == []


@pytest.mark.asyncio
async def test_each_admission_reserves_under_its_own_id():
    repo = _SlotRepo()
    gates = [_gate(repo), _gate(repo)]
    first, second = [await anext(gate) for gate in gates]

    assert first.id != second.id
    for gate in gates:
        await gate.aclose()


def test_helpers_ignore_a_stubbed_dependency():
    """Router tests override the dependency with ``lambda: None``."""
    assert file_slot_reservation_id(None) is None
    hand_off_file_slot(None)
