"""Regression test for #121 — PgUserRepository.create_user / create_legacy_user
must normalize empty external_user_id strings to NULL. Otherwise the second
insert raises UniqueViolation on the unique-but-nullable column.

We exercise the normalization logic directly without requiring a live
Postgres connection. The actual pool is replaced with a fake that records
the values passed to ``execute`` / ``fetchrow`` so we can assert the
binding parameter is ``None`` rather than ``''``.
"""

from __future__ import annotations

from typing import Any

import pytest


class _FakeRow(dict):
    """asyncpg returns Record-like objects; tests just need dict access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as e:
            raise AttributeError(name) from e


class _FakePool:
    def __init__(self):
        self.last_query: str | None = None
        self.last_params: tuple = ()
        self._next_row: _FakeRow | None = None
        self._rows: list[_FakeRow] = []

    def set_next_row(self, **fields):
        self._next_row = _FakeRow(fields)

    def set_rows(self, *rows: _FakeRow):
        self._rows = list(rows)

    async def fetchrow(self, query: str, *params):
        self.last_query = query
        self.last_params = params
        return self._next_row

    async def execute(self, query: str, *params):
        self.last_query = query
        self.last_params = params
        return "INSERT 0 1"

    # asyncpg.Pool.acquire returns a context manager — not exercised here

    async def fetch(self, query: str, *params):
        self.last_query = query
        self.last_params = params
        return self._rows


def _make_user_with_ext(ext: str | None):
    from core.models.user import User

    return User(
        display_name="x",
        external_user_id=ext,
        email=None,
        is_admin=False,
        file_quota=None,
        file_count=0,
    )


@pytest.mark.asyncio
async def test_create_user_coerces_empty_external_id_to_none():
    from services.persistence.user_repo import PgUserRepository

    pool = _FakePool()
    pool.set_next_row(
        id=42,
        display_name="x",
        external_user_id=None,
        email=None,
        token=None,
        is_admin=False,
        file_quota=None,
        file_count=0,
        created_at=__import__("datetime").datetime(2026, 1, 1),
    )
    repo = PgUserRepository(pool_getter=lambda: pool)

    await repo.create_user(_make_user_with_ext(""))
    # Position 2 is external_user_id in the INSERT (display_name=$1, external=$2)
    assert pool.last_params[1] is None, f"empty external_user_id was not coerced: {pool.last_params[1]!r}"

    # Same for whitespace-only
    await repo.create_user(_make_user_with_ext("   "))
    assert pool.last_params[1] is None


@pytest.mark.asyncio
async def test_create_user_preserves_real_external_id():
    from services.persistence.user_repo import PgUserRepository

    pool = _FakePool()
    pool.set_next_row(
        id=42,
        display_name="x",
        external_user_id="kc-alice",
        email=None,
        token=None,
        is_admin=False,
        file_quota=None,
        file_count=0,
        created_at=__import__("datetime").datetime(2026, 1, 1),
    )
    repo = PgUserRepository(pool_getter=lambda: pool)

    await repo.create_user(_make_user_with_ext("kc-alice"))
    assert pool.last_params[1] == "kc-alice"


@pytest.mark.asyncio
async def test_create_legacy_user_coerces_empty_external_id_to_none():
    from services.persistence.user_repo import PgUserRepository

    pool = _FakePool()
    pool.set_next_row(
        id=42,
        display_name="x",
        external_user_id=None,
        email=None,
        token="hash",
        is_admin=False,
        file_quota=None,
        file_count=0,
        created_at=__import__("datetime").datetime(2026, 1, 1),
    )
    repo = PgUserRepository(pool_getter=lambda: pool)

    await repo.create_legacy_user(
        display_name="x",
        external_user_id="",
        email=None,
        is_admin=False,
        file_quota=None,
    )
    # Same column position (display_name, external_user_id, ...)
    assert pool.last_params[1] is None


@pytest.mark.asyncio
async def test_list_users_dict_includes_email():
    from services.persistence.user_repo import PgUserRepository

    pool = _FakePool()
    pool.set_rows(
        _FakeRow(
            id=42,
            display_name="Alice",
            external_user_id="kc-alice",
            email="alice@example.com",
            is_admin=False,
            file_quota=None,
            file_count=0,
            created_at=__import__("datetime").datetime(2026, 1, 1),
        )
    )
    repo = PgUserRepository(pool_getter=lambda: pool)

    users = await repo.list_users_dict()

    assert users[0]["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_users_by_ids_fetches_all_users_in_one_query():
    from services.persistence.user_repo import PgUserRepository

    pool = _FakePool()
    pool.set_rows(
        _FakeRow(
            id=42,
            display_name="Alice",
            external_user_id="kc-alice",
            email="alice@example.com",
            token=None,
            is_admin=False,
            file_quota=None,
            file_count=0,
            created_at=__import__("datetime").datetime(2026, 1, 1),
        ),
        _FakeRow(
            id=84,
            display_name="Bob",
            external_user_id="kc-bob",
            email="bob@example.com",
            token=None,
            is_admin=False,
            file_quota=None,
            file_count=0,
            created_at=__import__("datetime").datetime(2026, 1, 2),
        ),
    )
    repo = PgUserRepository(pool_getter=lambda: pool)

    users = await repo.get_users_by_ids([42, 84])

    assert [user.id for user in users] == [42, 84]
    assert pool.last_params == ([42, 84],)
    assert "ANY($1::int[])" in (pool.last_query or "")
