"""Unit tests for PromptService.

Uses an in-memory fake repository so the resolution precedence, seeding, and
validation logic are tested without a database. Seeding runs against the *real*
bundled templates, which also verifies the prompt_type → config-key map lines
up with the on-disk filenames for all 8 types.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config.infrastructure import PathsConfig, PromptsConfig
from core.models.prompt import Prompt, PromptType
from core.utils.exceptions import NotFoundError, ValidationError
from services.orchestrators.prompt_service import PROMPT_TYPE_KEYS, PromptService


class FakePromptRepo:
    """Minimal in-memory PromptRepository honouring the same invariants."""

    def __init__(self) -> None:
        self.prompts: dict[str, Prompt] = {}

    async def create(self, prompt: Prompt) -> Prompt:
        if prompt.is_default:
            for p in self.prompts.values():
                if p.prompt_type == prompt.prompt_type:
                    p.is_default = False
        self.prompts[prompt.id] = prompt
        return prompt

    async def get(self, prompt_id: str) -> Prompt | None:
        return self.prompts.get(prompt_id)

    async def list(self, *, prompt_type=None, offset=0, limit=100) -> list[Prompt]:
        rows = [p for p in self.prompts.values() if prompt_type is None or p.prompt_type == prompt_type]
        rows.sort(key=lambda p: (p.prompt_type, p.name, p.created_at))
        return rows[offset : offset + limit]

    async def count(self, *, prompt_type=None) -> int:
        return len([p for p in self.prompts.values() if prompt_type is None or p.prompt_type == prompt_type])

    async def update(self, prompt_id: str, **fields) -> Prompt | None:
        p = self.prompts.get(prompt_id)
        if p is None:
            return None
        for k in ("name", "content"):
            if k in fields:
                setattr(p, k, fields[k])
        return p

    async def delete(self, prompt_id: str) -> bool:
        return self.prompts.pop(prompt_id, None) is not None

    async def get_by_name(self, prompt_type: str, name: str) -> Prompt | None:
        return next(
            (p for p in self.prompts.values() if p.prompt_type == prompt_type and p.name == name),
            None,
        )

    async def reference_counts(self) -> dict[tuple[str, str], int]:
        return getattr(self, "_ref_counts", {})

    async def get_default(self, prompt_type: str) -> Prompt | None:
        return next((p for p in self.prompts.values() if p.prompt_type == prompt_type and p.is_default), None)

    async def set_default(self, prompt_id: str) -> Prompt | None:
        target = self.prompts.get(prompt_id)
        if target is None:
            return None
        for p in self.prompts.values():
            if p.prompt_type == target.prompt_type:
                p.is_default = False
        target.is_default = True
        return target


def _service(repo: FakePromptRepo | None = None) -> PromptService:
    config = SimpleNamespace(paths=PathsConfig(), prompts=PromptsConfig())
    return PromptService(prompt_repo=repo or FakePromptRepo(), config=config)


class TestSeeding:
    async def test_seeds_all_types_from_disk(self):
        repo = FakePromptRepo()
        await _service(repo).seed_defaults()
        seeded_types = {p.prompt_type for p in repo.prompts.values()}
        assert seeded_types == set(PROMPT_TYPE_KEYS)
        assert len(PROMPT_TYPE_KEYS) == 7
        for p in repo.prompts.values():
            assert p.is_default is True
            assert p.content.strip()

    async def test_seeding_is_idempotent(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        await svc.seed_defaults()
        sysp = await repo.get_default("sys_prompt")
        sysp.content = "OPERATOR EDIT"
        await svc.seed_defaults()
        assert (await repo.get_default("sys_prompt")).content == "OPERATOR EDIT"
        assert len(repo.prompts) == 7

    async def test_type_set_matches_enum(self):
        assert set(PROMPT_TYPE_KEYS) == {t.value for t in PromptType}


class TestResolution:
    async def test_precedence_named_then_default_then_disk(self):
        repo = FakePromptRepo()
        svc = _service(repo)

        # Nothing in DB → disk seed fallback (never empty).
        disk = await svc.resolve_prompt("sys_prompt")
        assert disk.strip()

        # Global default → wins over disk.
        await repo.create(Prompt(prompt_type="sys_prompt", name="default_sys", content="DEFAULT", is_default=True))
        assert await svc.resolve_prompt("sys_prompt") == "DEFAULT"

        # A named prompt → wins over default when named.
        await repo.create(Prompt(prompt_type="sys_prompt", name="legal", content="LEGAL"))
        assert await svc.resolve_prompt("sys_prompt", names=["legal"]) == "LEGAL"
        # Unknown / None names are skipped, falling through to the default.
        assert await svc.resolve_prompt("sys_prompt", names=["missing"]) == "DEFAULT"
        assert await svc.resolve_prompt("sys_prompt", names=[None]) == "DEFAULT"

    async def test_first_resolvable_name_wins(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        await repo.create(Prompt(prompt_type="hyde", name="b", content="B"))
        # Ordered candidates: first that resolves wins (extension point for a
        # future per-user tier prepended ahead of the partition/preset name).
        assert await svc.resolve_prompt("hyde", names=["a", "b"]) == "B"


class TestCrud:
    async def test_create_validates_type(self):
        with pytest.raises(ValidationError):
            await _service().create_prompt(prompt_type="not_a_type", name="x", content="y")

    async def test_get_missing_raises(self):
        with pytest.raises(NotFoundError):
            await _service().get_prompt("nope")

    async def test_create_duplicate_name_per_type_is_rejected(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        await svc.create_prompt(prompt_type="sys_prompt", name="formal", content="a")
        with pytest.raises(ValidationError):
            await svc.create_prompt(prompt_type="sys_prompt", name="formal", content="b")
        # Same name under a different type is fine.
        await svc.create_prompt(prompt_type="hyde", name="formal", content="c")

    async def test_rename_collision_is_rejected(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        await svc.create_prompt(prompt_type="sys_prompt", name="a", content="a")
        b = await svc.create_prompt(prompt_type="sys_prompt", name="b", content="b")
        with pytest.raises(ValidationError):
            await svc.update_prompt(b.id, name="a")

    async def test_update_promotes_default_via_set_default(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        a = await repo.create(Prompt(prompt_type="sys_prompt", name="a", content="a", is_default=True))
        b = await repo.create(Prompt(prompt_type="sys_prompt", name="b", content="b"))
        updated = await svc.update_prompt(b.id, content="b2", is_default=True)
        assert updated.is_default is True and updated.content == "b2"
        assert (await repo.get(a.id)).is_default is False  # single-default invariant

    async def test_delete_default_is_rejected(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        d = await repo.create(Prompt(prompt_type="sys_prompt", content="c", is_default=True))
        with pytest.raises(ValidationError):
            await svc.delete_prompt(d.id)

    async def test_delete_non_default_ok(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        p = await repo.create(Prompt(prompt_type="sys_prompt", content="c"))
        await svc.delete_prompt(p.id)
        assert await repo.get(p.id) is None

    async def test_set_default_missing_raises(self):
        with pytest.raises(NotFoundError):
            await _service().set_default("nope")

    async def test_list_filters_and_annotates_used_by(self):
        repo = FakePromptRepo()
        svc = _service(repo)
        p = await repo.create(Prompt(prompt_type="hyde", name="b", content="c"))
        repo._ref_counts = {("hyde", "b"): 3}
        listed = await svc.list_prompts(prompt_type="hyde")
        assert [row["prompt_type"] for row in listed] == ["hyde"]
        assert listed[0]["used_by"] == 3
        assert p.id == listed[0]["id"]
