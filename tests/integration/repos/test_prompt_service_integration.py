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

    async def test_reference_counts_are_partition_centric(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        partition_repo = postgres_store.partition_repo
        # An indexation preset naming a prompt, plus one that no partition uses.
        await postgres_store.preset_repo.upsert("legal", "indexation", {"contextualization_prompt_name": "ctx1"})
        await postgres_store.preset_repo.upsert("orphan", "indexation", {"contextualization_prompt_name": "ghost"})
        # Two partitions select the "legal" preset; one of them also names a
        # generation prompt directly.
        await partition_repo.create_partition("rc1")
        await partition_repo.update_partition(
            "rc1", indexation_preset="legal", generation_prompt_names={"sys_prompt": "formal"}
        )
        await partition_repo.create_partition("rc2")
        await partition_repo.update_partition("rc2", indexation_preset="legal")

        counts = await repo.reference_counts()

        # Generation prompt: the one partition that names it directly.
        assert counts.get(("sys_prompt", "formal")) == 1
        # Indexation prompt: counted per *partition* that selects the preset (2),
        # not per preset (1).
        assert counts.get(("chunk_contextualizer", "ctx1")) == 2
        # A preset naming a prompt but selected by no partition contributes nothing.
        assert counts.get(("chunk_contextualizer", "ghost")) is None
