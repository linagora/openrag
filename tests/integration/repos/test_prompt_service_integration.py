"""PromptService against a real Postgres via PgPromptRepository.

Covers the boot-critical seam end-to-end: seed_defaults writes real rows, and
resolve_prompt resolves named prompt → default → disk against a live DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.config.infrastructure import PathsConfig, PromptsConfig
from core.models.prompt import Prompt
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

    async def test_reference_counts_are_effective(self, postgres_store: PostgresStore):
        repo = postgres_store.prompt_repo
        partition_repo = postgres_store.partition_repo

        # Library: a default + a named alternative for two types.
        await repo.create(Prompt(prompt_type="sys_prompt", name="d_sys", content="x", is_default=True))
        await repo.create(Prompt(prompt_type="sys_prompt", name="legal", content="x"))
        await repo.create(Prompt(prompt_type="chunk_contextualizer", name="d_ctx", content="x", is_default=True))
        await repo.create(Prompt(prompt_type="chunk_contextualizer", name="ctx1", content="x"))
        await repo.create(Prompt(prompt_type="asr_transcription", name="d_asr", content="x", is_default=True))
        await repo.create(Prompt(prompt_type="asr_transcription", name="meeting", content="x"))

        # An indexation preset naming ctx1, plus one naming a non-existent prompt.
        await postgres_store.preset_repo.upsert("legalpreset", "indexation", {"contextualization_prompt_name": "ctx1"})
        await postgres_store.preset_repo.upsert("orphan", "indexation", {"contextualization_prompt_name": "ghost"})

        # 3 partitions: rc1 overrides sys_prompt and ASR, and uses legalpreset;
        # rc2 uses legalpreset with no partition override; rc3 names missing
        # sys_prompt and ASR prompts (both fall back to their defaults).
        await partition_repo.create_partition("rc1")
        await partition_repo.update_partition(
            "rc1",
            indexation_preset="legalpreset",
            generation_prompt_names={"sys_prompt": "legal", "asr_transcription": "meeting"},
        )
        await partition_repo.create_partition("rc2")
        await partition_repo.update_partition("rc2", indexation_preset="legalpreset")
        await partition_repo.create_partition("rc3")
        await partition_repo.update_partition(
            "rc3", generation_prompt_names={"sys_prompt": "missing", "asr_transcription": "missing"}
        )

        counts = await repo.reference_counts()

        # sys_prompt: rc1 -> legal; rc2 (no override) + rc3 (dangling) fall back to default.
        assert counts.get(("sys_prompt", "legal")) == 1
        assert counts.get(("sys_prompt", "d_sys")) == 2
        # chunk_contextualizer: rc1 + rc2 -> ctx1 (via legalpreset); rc3 -> default.
        assert counts.get(("chunk_contextualizer", "ctx1")) == 2
        assert counts.get(("chunk_contextualizer", "d_ctx")) == 1
        # asr_transcription: rc1 selects meeting; rc2 (no selection) and rc3
        # (dangling selection) use the ASR default.
        assert counts.get(("asr_transcription", "meeting")) == 1
        assert counts.get(("asr_transcription", "d_asr")) == 2
        # The "orphan" preset names a non-existent prompt and is used by no
        # partition — it contributes to nothing.
        assert counts.get(("chunk_contextualizer", "ghost")) is None
        # Per type the effective counts sum to the partition total (3).
        assert counts[("sys_prompt", "legal")] + counts[("sys_prompt", "d_sys")] == 3
        assert counts[("chunk_contextualizer", "ctx1")] + counts[("chunk_contextualizer", "d_ctx")] == 3
        assert counts[("asr_transcription", "meeting")] + counts[("asr_transcription", "d_asr")] == 3
