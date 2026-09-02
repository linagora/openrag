"""Unit tests for prompt-reference updates in PgPromptRepository."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _AsyncCtx:
    def __init__(self, value) -> None:
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_):
        return False


class _FakePool:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self._fetchrows = [
            _prompt_row(name="meeting-notes"),
            _prompt_row(name="meeting-notes-v2"),
        ]

    def acquire(self):
        return _AsyncCtx(self)

    def transaction(self):
        return _AsyncCtx(self)

    async def fetchrow(self, query: str, *params):
        self.executed.append((query, params))
        return self._fetchrows.pop(0)

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "UPDATE 1"


class _ReferenceCountsPool:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetchval(self, query: str) -> int:
        self.queries.append(query)
        return 0

    async def fetch(self, query: str):
        self.queries.append(query)
        return []


def _prompt_row(*, name: str) -> dict:
    return {
        "id": "prompt-id",
        "prompt_type": "asr_transcription",
        "name": name,
        "content": "Keep speaker labels.",
        "is_default": False,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


@pytest.mark.asyncio
async def test_renaming_asr_prompt_updates_referencing_indexation_presets() -> None:
    """A missing cascade would strand strict ASR selections after a rename."""
    from services.persistence.prompt_repo import PgPromptRepository

    pool = _FakePool()
    repo = PgPromptRepository(pool_getter=lambda: pool)

    await repo.update("prompt-id", name="meeting-notes-v2")

    cascades = [(query, params) for query, params in pool.executed if "UPDATE pipeline_presets" in query]
    assert len(cascades) == 1
    query, params = cascades[0]
    assert "btrim(config->>$4) = $5" in query
    assert params == (
        ["asr_transcription_prompt_name"],
        "meeting-notes-v2",
        "indexation",
        "asr_transcription_prompt_name",
        "meeting-notes",
    )


@pytest.mark.asyncio
async def test_reference_counts_normalizes_asr_preset_selection_names() -> None:
    from services.persistence.prompt_repo import PgPromptRepository

    pool = _ReferenceCountsPool()
    repo = PgPromptRepository(pool_getter=lambda: pool)

    await repo.reference_counts()

    preset_query = next(query for query in pool.queries if "FROM partitions part" in query)
    assert "WHEN c.key = 'asr_transcription_prompt_name' THEN btrim(c.value)" in preset_query
