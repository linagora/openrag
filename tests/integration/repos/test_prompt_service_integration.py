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

        # An indexation preset naming ctx1 and the meeting ASR prompt, plus one
        # naming non-existent alternatives.
        await postgres_store.preset_repo.upsert(
            "legalpreset",
            "indexation",
            {"contextualization_prompt_name": "ctx1", "asr_transcription_prompt_name": "meeting"},
        )
        await postgres_store.preset_repo.upsert(
            "orphan",
            "indexation",
            {"contextualization_prompt_name": "ghost", "asr_transcription_prompt_name": "missing"},
        )

        # 3 partitions: rc1 overrides its final-answer prompt and uses
        # legalpreset; rc2 also uses legalpreset; rc3 names a missing final-answer
        # prompt and uses orphan, so both of its types fall back to the default.
        await partition_repo.create_partition("rc1")
        await partition_repo.update_partition(
            "rc1", indexation_preset="legalpreset", generation_prompt_names={"sys_prompt": "legal"}
        )
        await partition_repo.create_partition("rc2")
        await partition_repo.update_partition("rc2", indexation_preset="legalpreset")
        await partition_repo.create_partition("rc3")
        await partition_repo.update_partition(
            "rc3", indexation_preset="orphan", generation_prompt_names={"sys_prompt": "missing"}
        )

        counts = await repo.reference_counts()

        # sys_prompt: rc1 -> legal; rc2 (no override) + rc3 (dangling) fall back to default.
        assert counts.get(("sys_prompt", "legal")) == 1
        assert counts.get(("sys_prompt", "d_sys")) == 2
        # chunk_contextualizer: rc1 + rc2 -> ctx1 (via legalpreset); rc3 -> default.
        assert counts.get(("chunk_contextualizer", "ctx1")) == 2
        assert counts.get(("chunk_contextualizer", "d_ctx")) == 1
        # asr_transcription: rc1 + rc2 select meeting through legalpreset;
        # rc3's dangling preset selection falls back to the ASR default.
        assert counts.get(("asr_transcription", "meeting")) == 2
        assert counts.get(("asr_transcription", "d_asr")) == 1
        # The orphan preset's stale prompt name contributes nothing.
        assert counts.get(("chunk_contextualizer", "ghost")) is None
        # Per type the effective counts sum to the partition total (3).
        assert counts[("sys_prompt", "legal")] + counts[("sys_prompt", "d_sys")] == 3
        assert counts[("chunk_contextualizer", "ctx1")] + counts[("chunk_contextualizer", "d_ctx")] == 3
        assert counts[("asr_transcription", "meeting")] + counts[("asr_transcription", "d_asr")] == 3
