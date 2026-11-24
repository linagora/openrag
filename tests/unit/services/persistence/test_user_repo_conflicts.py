"""A duplicate email or external ID is a 409 whose message never quotes the value.

asyncpg's ``UniqueViolationError`` carries the duplicate in its message
(``DETAIL: Key (email)=(…) already exists``). Raised out of the repository it
reached the 500 handler, which logs the message, and the OIDC provisioning
path, which logs it with ``logger.exception``.
"""

from __future__ import annotations

import asyncpg
import pytest
from core.models.user import User
from core.utils.exceptions import ConflictError
from loguru import logger
from services.persistence.user_repo import PgUserRepository

EMAIL = "jeanne.dupontel@example.org"


def _violation(constraint: str, column: str, value: str) -> asyncpg.UniqueViolationError:
    exc = asyncpg.UniqueViolationError(
        f'duplicate key value violates unique constraint "{constraint}"\n'
        f"DETAIL:  Key ({column})=({value}) already exists."
    )
    exc.constraint_name = constraint
    return exc


class _RaisingPool:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def fetchrow(self, query: str, *params):
        raise self.exc

    async def execute(self, query: str, *params):
        raise self.exc


def _repo(exc: Exception) -> PgUserRepository:
    pool = _RaisingPool(exc)
    return PgUserRepository(pool_getter=lambda: pool)


WRITES = {
    "create_user": lambda repo: repo.create_user(User(display_name="Jeanne", email=EMAIL)),
    "create_legacy_user": lambda repo: repo.create_legacy_user(
        display_name="Jeanne", external_user_id=None, email=EMAIL, is_admin=False, file_quota=None
    ),
    "update_user": lambda repo: repo.update_user(7, email=EMAIL),
    "update_user_fields": lambda repo: repo.update_user_fields(7, {"email": EMAIL}),
}


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
async def test_a_duplicate_email_is_a_conflict_without_the_address(write):
    with pytest.raises(ConflictError) as raised:
        await write(_repo(_violation("ix_users_email", "email", EMAIL)))

    assert raised.value.status_code == 409
    assert "Another user already has this email address." in str(raised.value)
    assert EMAIL not in str(raised.value)
    # Not chained: a logged traceback would print the database error too.
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


async def test_a_duplicate_external_id_names_that_field():
    with pytest.raises(ConflictError, match="this external user ID"):
        await WRITES["create_user"](_repo(_violation("ix_users_external_user_id", "external_user_id", "kc-7731")))


async def test_an_unknown_unique_constraint_still_hides_the_value():
    with pytest.raises(ConflictError) as raised:
        await WRITES["create_user"](_repo(_violation("users_token_key", "token", "secret-hash")))

    assert "secret-hash" not in str(raised.value)


async def test_a_logged_conflict_carries_no_address():
    """What ``logger.exception`` writes, as the OIDC provisioning path does."""
    lines: list[str] = []
    sink_id = logger.add(lines.append, format="{message}\n{exception}", diagnose=False)
    try:
        try:
            await WRITES["create_user"](_repo(_violation("ix_users_email", "email", EMAIL)))
        except ConflictError as e:
            logger.exception(f"OIDC auto-provisioning failed: {e}")
    finally:
        logger.remove(sink_id)

    assert lines
    assert EMAIL not in "".join(lines)
