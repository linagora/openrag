"""PgPromptRepository against a real Postgres.

Also exercises the migration end-to-end: the ``postgres_store`` fixture runs
``e8f9a0b1c2d3_add_prompts_library`` before any of these can pass.
"""

from __future__ import annotations

import pytest
from core.models.prompt import Prompt
from core.utils.exceptions import ValidationError
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _prompt(
    prompt_type: str = "sys_prompt", *, name: str = "p", content: str = "body", is_default: bool = False
) -> Prompt:
    return Prompt(prompt_type=prompt_type, name=name, content=content, is_default=is_default)


class TestCrud:
    async def test_create_then_get(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        created = await repo.create(_prompt(name="hello", content="world"))
        fetched = await repo.get(created.id)
        assert fetched is not None
        assert (fetched.name, fetched.content, fetched.prompt_type) == ("hello", "world", "sys_prompt")
        # Timestamps come from the DB default.
        assert fetched.created_at is not None and fetched.updated_at is not None

    async def test_get_missing_returns_none(self, postgres_store: PostgresStore):
        assert await postgres_store.prompt_repo.get("no-such-id") is None

    async def test_list_filter_and_paginate(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        await repo.create(_prompt("sys_prompt", name="a"))
        await repo.create(_prompt("sys_prompt", name="b"))
        await repo.create(_prompt("hyde", name="c"))
        assert {p.name for p in await repo.list(prompt_type="sys_prompt")} == {"a", "b"}
        assert len(await repo.list()) == 3
        assert len(await repo.list(offset=1, limit=1)) == 1

    async def test_count(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        await repo.create(_prompt("sys_prompt"))
        await repo.create(_prompt("hyde"))
        assert await repo.count() == 2
        assert await repo.count(prompt_type="hyde") == 1

    async def test_update_name_and_content(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        created = await repo.create(_prompt(name="old", content="old-body"))
        updated = await repo.update(created.id, name="new", content="new-body")
        assert updated is not None
        assert (updated.name, updated.content) == ("new", "new-body")

    async def test_update_ignores_non_whitelisted_fields(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        created = await repo.create(_prompt(prompt_type="sys_prompt", is_default=False))
        # is_default / prompt_type must not be writable through update().
        updated = await repo.update(created.id, is_default=True, prompt_type="hyde", name="ok")
        assert updated is not None
        assert updated.is_default is False
        assert updated.prompt_type == "sys_prompt"
        assert updated.name == "ok"

    async def test_delete(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        created = await repo.create(_prompt())
        assert await repo.delete(created.id) is True
        assert await repo.delete(created.id) is False
        assert await repo.get(created.id) is None


class TestGetByName:
    async def test_get_by_name(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        created = await repo.create(_prompt("sys_prompt", name="legal"))
        found = await repo.get_by_name("sys_prompt", "legal")
        assert found is not None and found.id == created.id
        # Scoped by type, and None when the name doesn't exist.
        assert await repo.get_by_name("hyde", "legal") is None
        assert await repo.get_by_name("sys_prompt", "missing") is None


class TestDefaultPerType:
    async def test_get_default_none_when_absent(self, postgres_store: PostgresStore):
        assert await postgres_store.prompt_repo.get_default("sys_prompt") is None

    async def test_create_default_is_returned_by_get_default(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        created = await repo.create(_prompt(is_default=True))
        got = await repo.get_default("sys_prompt")
        assert got is not None and got.id == created.id

    async def test_second_default_demotes_first(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        first = await repo.create(_prompt(name="first", is_default=True))
        second = await repo.create(_prompt(name="second", is_default=True))
        # Only the second is default now; the invariant (one default/type) holds.
        assert (await repo.get_default("sys_prompt")).id == second.id
        assert (await repo.get(first.id)).is_default is False

    async def test_set_default_clears_previous(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        a = await repo.create(_prompt(name="a", is_default=True))
        b = await repo.create(_prompt(name="b"))
        promoted = await repo.set_default(b.id)
        assert promoted is not None and promoted.is_default is True
        assert (await repo.get_default("sys_prompt")).id == b.id
        assert (await repo.get(a.id)).is_default is False

    async def test_set_default_missing_returns_none(self, postgres_store: PostgresStore):
        assert await postgres_store.prompt_repo.set_default("nope") is None

    async def test_default_is_scoped_per_type(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        sysp = await repo.create(_prompt("sys_prompt", is_default=True))
        hyde = await repo.create(_prompt("hyde", is_default=True))
        # Two defaults coexist because they are different types.
        assert (await repo.get_default("sys_prompt")).id == sysp.id
        assert (await repo.get_default("hyde")).id == hyde.id

    async def test_duplicate_name_raises_409_not_500(self, postgres_store: PostgresStore):
        """The service's pre-check is not atomic: a concurrent create can still
        reach the unique index. The repo must translate that into the same 409
        the sequential path returns, not let a UniqueViolationError become a 500.
        """
        repo = postgres_store.prompt_repo
        await repo.create(_prompt(name="clash"))
        with pytest.raises(ValidationError) as err:
            await repo.create(_prompt(name="clash"))
        assert err.value.status_code == 409

    async def test_rename_onto_an_existing_name_raises_409(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        await repo.create(_prompt(name="taken"))
        other = await repo.create(_prompt(name="free"))
        with pytest.raises(ValidationError) as err:
            await repo.update(other.id, name="taken")
        assert err.value.status_code == 409
