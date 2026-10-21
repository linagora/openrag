"""Preset cache revision integration tests against real PostgreSQL."""

from __future__ import annotations

import pytest
from core.models.prompt import Prompt
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _asr_prompt(name: str) -> Prompt:
    return Prompt(prompt_type="asr_transcription", name=name, content="Transcribe faithfully.", is_default=False)


class TestPresetConfigurationRevision:
    async def test_normal_preset_crud_advances_the_revision(self, postgres_store: PostgresStore):
        repo = postgres_store.preset_repo
        before = await repo.latest_revision()

        await repo.upsert("revision-crud", "indexation", {"enable_topic_tagging": False})
        after_create = await repo.latest_revision()
        await repo.upsert("revision-crud", "indexation", {"enable_topic_tagging": True})
        after_update = await repo.latest_revision()
        await repo.rename("revision-crud", "revision-crud-v2", "indexation", {"enable_topic_tagging": True})
        after_rename = await repo.latest_revision()
        assert await repo.delete("revision-crud-v2", "indexation") is True

        assert before < after_create < after_update < after_rename < await repo.latest_revision()

    async def test_asr_prompt_cascades_advance_the_revision(self, postgres_store: PostgresStore):
        preset_repo = postgres_store.preset_repo
        prompt_repo = postgres_store.prompt_repo
        prompt = await prompt_repo.create(_asr_prompt("revision-asr"))
        await preset_repo.upsert(
            "revision-asr-preset",
            "indexation",
            {"asr_transcription_prompt_name": "revision-asr"},
        )

        before_rename = await preset_repo.latest_revision()
        renamed = await prompt_repo.update(prompt.id, name="revision-asr-v2")
        after_rename = await preset_repo.latest_revision()
        assert renamed is not None

        before_delete = after_rename
        assert await prompt_repo.delete(prompt.id) is True
        after_delete = await preset_repo.latest_revision()

        assert after_rename == before_rename + 1
        assert after_delete == before_delete + 1

    async def test_endpoint_rename_cascade_advances_the_revision(self, postgres_store: PostgresStore):
        preset_repo = postgres_store.preset_repo
        async with postgres_store.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO model_endpoints (name, model_type, endpoint, model_name, batch_size, timeout, extra, is_default)
                VALUES ('revision-stt', 'stt', 'http://stt.example/v1', 'speech', 1, 30, '{}'::jsonb, false)
                """
            )
        await preset_repo.upsert("revision-stt-preset", "indexation", {"stt": "revision-stt"})

        before = await preset_repo.latest_revision()
        await postgres_store.model_endpoint_repo.rename("revision-stt", "stt", "revision-stt-v2")

        assert await preset_repo.latest_revision() == before + 1
        preset = await preset_repo.get("revision-stt-preset", "indexation")
        assert preset is not None and preset["config"]["stt"] == "revision-stt-v2"

    async def test_earlier_transaction_committing_last_advances_revision(self, postgres_store: PostgresStore):
        """A delayed ASR cascade must be visible after a newer transaction commits.

        ``now()`` would stamp the earlier transaction with an older timestamp,
        even though it commits after the unrelated update. The trigger's row
        lock instead serializes revisions in visibility order.
        """
        repo = postgres_store.preset_repo
        await repo.upsert(
            "revision-delayed-asr",
            "indexation",
            {"asr_transcription_prompt_name": "old-asr"},
        )
        await repo.upsert("revision-unrelated", "retrieval", {"top_k": 10})
        before = await repo.latest_revision()

        async with postgres_store.pool.acquire() as earlier, postgres_store.pool.acquire() as later:
            async with earlier.transaction():
                await earlier.execute("SELECT 1")  # establish the earlier transaction first
                await later.execute(
                    "UPDATE pipeline_presets SET config = jsonb_set(config, '{top_k}', '20'::jsonb) "
                    "WHERE name = 'revision-unrelated' AND preset_type = 'retrieval'"
                )
                observed_after_later_commit = await repo.latest_revision()
                await earlier.execute(
                    "UPDATE pipeline_presets "
                    "SET config = jsonb_set(config, '{asr_transcription_prompt_name}', '\"new-asr\"'::jsonb) "
                    "WHERE name = 'revision-delayed-asr' AND preset_type = 'indexation'"
                )

        assert observed_after_later_commit == before + 1
        assert await repo.latest_revision() == observed_after_later_commit + 1
