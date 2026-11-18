"""On a real, migrated database, a duplicate is a 409 naming the field, not the value.

The repository maps a unique violation to a message by the constraint's name.
These tests pin the names PostgreSQL actually reports for the users table, so a
renamed index shows up here rather than as a vaguer message in production.
"""

from __future__ import annotations

import pytest
from core.models.user import User
from core.utils.exceptions import ConflictError
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

EMAIL = "jeanne.dupontel@example.org"


async def test_a_duplicate_email_names_the_field_only(postgres_store: PostgresStore):
    repo = postgres_store.user_repo
    await repo.create_user(User(display_name="Jeanne", email=EMAIL))

    with pytest.raises(ConflictError) as raised:
        await repo.create_legacy_user(
            display_name="Other", external_user_id=None, email=EMAIL, is_admin=False, file_quota=None
        )

    assert "this email address" in str(raised.value)
    assert EMAIL not in str(raised.value)


async def test_a_duplicate_external_id_names_the_field_only(postgres_store: PostgresStore):
    repo = postgres_store.user_repo
    await repo.create_user(User(display_name="Jeanne", external_user_id="kc-jeanne-7731"))

    with pytest.raises(ConflictError) as raised:
        await repo.create_user(User(display_name="Other", external_user_id="kc-jeanne-7731"))

    assert "this external user ID" in str(raised.value)
    assert "kc-jeanne-7731" not in str(raised.value)
