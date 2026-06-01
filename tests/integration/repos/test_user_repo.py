"""Phase 7F — PgUserRepository against a real Postgres."""

from __future__ import annotations

import pytest
from core.models.user import User
from services.persistence.user_repo import _hash_token
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _user(**overrides) -> User:
    defaults = {
        "display_name": "Alice",
        "email": "alice@example.com",
        "is_admin": False,
    }
    defaults.update(overrides)
    return User(**defaults)


class TestCreateGet:
    async def test_create_returns_assigned_id(self, postgres_store: PostgresStore):
        created = await postgres_store.user_repo.create_user(_user())
        assert created.id > 0
        assert created.display_name == "Alice"
        assert created.email == "alice@example.com"

    async def test_get_by_id(self, postgres_store: PostgresStore):
        created = await postgres_store.user_repo.create_user(_user(display_name="Bob"))
        fetched = await postgres_store.user_repo.get_user(created.id)
        assert fetched is not None
        assert fetched.display_name == "Bob"

    async def test_get_missing_returns_none(self, postgres_store: PostgresStore):
        assert await postgres_store.user_repo.get_user(9999) is None

    async def test_email_lowercased_on_insert(self, postgres_store: PostgresStore):
        created = await postgres_store.user_repo.create_user(
            _user(email="MIXED@Example.COM"),
        )
        assert created.email == "mixed@example.com"

    async def test_get_by_email_case_insensitive(self, postgres_store: PostgresStore):
        await postgres_store.user_repo.create_user(
            _user(email="carol@example.com"),
        )
        # Lookup uppercases the input — the repo normalises.
        fetched = await postgres_store.user_repo.get_user_by_email("CAROL@Example.com")
        assert fetched is not None
        assert fetched.email == "carol@example.com"

    async def test_get_by_external_id(self, postgres_store: PostgresStore):
        await postgres_store.user_repo.create_user(
            _user(external_user_id="kc-alice-uuid"),
        )
        fetched = await postgres_store.user_repo.get_user_by_external_id("kc-alice-uuid")
        assert fetched is not None
        assert fetched.external_user_id == "kc-alice-uuid"


class TestLegacyTokenFlow:
    async def test_create_legacy_user_returns_plaintext_token(
        self,
        postgres_store: PostgresStore,
    ):
        result = await postgres_store.user_repo.create_legacy_user(
            display_name="Tokened",
            external_user_id=None,
            email=None,
            is_admin=False,
            file_quota=None,
        )
        assert result["token"].startswith("or-")
        # ``"or-"`` (3 chars) + ``secrets.token_hex(16)`` (32 hex chars) = 35.
        assert len(result["token"]) == 35

    async def test_get_user_by_token_hash_roundtrip(
        self,
        postgres_store: PostgresStore,
    ):
        created = await postgres_store.user_repo.create_legacy_user(
            display_name="Tokened2",
            external_user_id=None,
            email=None,
            is_admin=False,
            file_quota=None,
        )
        looked_up = await postgres_store.user_repo.get_user_by_token(
            _hash_token(created["token"]),
        )
        assert looked_up is not None
        assert looked_up.id == created["id"]

    async def test_regenerate_token_invalidates_old(
        self,
        postgres_store: PostgresStore,
    ):
        created = await postgres_store.user_repo.create_legacy_user(
            display_name="Tokened3",
            external_user_id=None,
            email=None,
            is_admin=False,
            file_quota=None,
        )
        new = await postgres_store.user_repo.regenerate_user_token(created["id"])
        assert new is not None
        assert new["token"] != created["token"]
        # old hash no longer resolves
        assert (
            await postgres_store.user_repo.get_user_by_token(
                _hash_token(created["token"]),
            )
            is None
        )


class TestUpdateDelete:
    async def test_update_user_fields(self, postgres_store: PostgresStore):
        created = await postgres_store.user_repo.create_user(_user())
        updated = await postgres_store.user_repo.update_user(
            created.id,
            display_name="Renamed",
        )
        assert updated is not None
        assert updated.display_name == "Renamed"

    async def test_unknown_field_is_ignored(self, postgres_store: PostgresStore):
        created = await postgres_store.user_repo.create_user(_user())
        # ``password_hash`` is in the domain model but not the schema —
        # the repo silently ignores it instead of failing.
        updated = await postgres_store.user_repo.update_user(
            created.id,
            password_hash="ignored",
        )
        assert updated is not None
        assert updated.display_name == "Alice"

    async def test_delete_returns_true(self, postgres_store: PostgresStore):
        created = await postgres_store.user_repo.create_user(_user())
        assert await postgres_store.user_repo.delete_user(created.id) is True
        assert await postgres_store.user_repo.get_user(created.id) is None

    async def test_count_users(self, postgres_store: PostgresStore):
        assert await postgres_store.user_repo.count_users() == 0
        await postgres_store.user_repo.create_user(_user(display_name="A"))
        await postgres_store.user_repo.create_user(
            _user(display_name="B", email="b@example.com"),
        )
        assert await postgres_store.user_repo.count_users() == 2
