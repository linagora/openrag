"""PromptService against a real Postgres via PgPromptRepository.

Covers the boot-critical seam end-to-end: seed_defaults writes real rows, and
resolve_prompt resolves named prompt → default → disk against a live DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config.infrastructure import PathsConfig, PromptsConfig
from services.orchestrators.prompt_service import PROMPT_TYPE_KEYS, PromptService
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _service(store: PostgresStore) -> PromptService:
    config = SimpleNamespace(paths=PathsConfig(), prompts=PromptsConfig())
    return PromptService(prompt_repo=store.prompt_repo, config=config)


class TestSeedAndResolve:
    async def test_seed_defaults_creates_one_default_per_type(self, postgres_store: PostgresStore):
        svc = _service(postgres_store)
        await svc.seed_defaults()
        assert await postgres_store.prompt_repo.count() == len(PROMPT_TYPE_KEYS)
        for prompt_type in PROMPT_TYPE_KEYS:
            default = await postgres_store.prompt_repo.get_default(prompt_type)
            assert default is not None and default.content.strip()

    async def test_seed_is_idempotent(self, postgres_store: PostgresStore):
        svc = _service(postgres_store)
        await svc.seed_defaults()
        await svc.seed_defaults()
        assert await postgres_store.prompt_repo.count() == len(PROMPT_TYPE_KEYS)

    async def test_resolution_precedence_end_to_end(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        svc = _service(postgres_store)
        await svc.seed_defaults()

        seeded = (await repo.get_default("sys_prompt")).content

        # No candidate names → the seeded default resolves.
        assert await svc.resolve_prompt("sys_prompt") == seeded
        assert await svc.resolve_prompt("sys_prompt", names=["missing"]) == seeded

        # A named library prompt wins when named.
        await svc.create_prompt(prompt_type="sys_prompt", name="legal", content="LEGAL")
        assert await svc.resolve_prompt("sys_prompt", names=["legal"]) == "LEGAL"
        # First resolvable candidate wins (the per-user tier extension point).
        assert await svc.resolve_prompt("sys_prompt", names=["missing", "legal"]) == "LEGAL"
