from __future__ import annotations

import pytest
from services.persistence.user_repo import _hash_token


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, existing: dict | None):
        self.existing = existing
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *params):
        if "SELECT id, token FROM users WHERE id = 1" in query:
            return self.existing
        return None

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "UPDATE 1"


class _FakePool:
    def __init__(self, existing: dict | None):
        self.conn = _FakeConn(existing)

    def acquire(self):
        return _AsyncContext(self.conn)


@pytest.mark.asyncio
async def test_first_call_creates_admin_with_provided_token():
    from services.persistence.user_repo import PgUserRepository

    pool = _FakePool(existing=None)
    repo = PgUserRepository(pool_getter=lambda: pool)

    assert await repo.ensure_admin_user("or-static-token") == "or-static-token"
    assert any(
        "INSERT INTO users" in query and params == (_hash_token("or-static-token"),)
        for query, params in pool.conn.executed
    )


@pytest.mark.asyncio
async def test_restart_with_no_auth_token_preserves_existing_token():
    from services.persistence.user_repo import PgUserRepository

    pool = _FakePool(existing={"id": 1, "token": _hash_token("or-rotated")})
    repo = PgUserRepository(pool_getter=lambda: pool)

    assert await repo.ensure_admin_user("") == ""
    assert any("UPDATE users SET is_admin = TRUE WHERE id = 1" in query for query, _ in pool.conn.executed)
    assert not any("token =" in query for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_restart_with_auth_token_syncs_db_to_env():
    from services.persistence.user_repo import PgUserRepository

    pool = _FakePool(existing={"id": 1, "token": _hash_token("or-old")})
    repo = PgUserRepository(pool_getter=lambda: pool)

    assert await repo.ensure_admin_user("or-new") == "or-new"
    assert any(
        "UPDATE users SET is_admin = TRUE, token = $1 WHERE id = 1" in query and params == (_hash_token("or-new"),)
        for query, params in pool.conn.executed
    )
