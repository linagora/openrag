"""Tests for persisted preset and partition domain row invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig, PartitionRow, PresetRow, resolve_partition_chat_llm
from pydantic import ValidationError


def _now() -> datetime:
    """Return a timestamp for persisted row test payloads."""
    return datetime.now(UTC)


def test_preset_row_rejects_unknown_preset_type():
    """Persisted preset rows reject unknown preset types."""
    now = _now()

    with pytest.raises(ValidationError):
        PresetRow(
            name="default",
            preset_type="unknown",
            config={},
            created_at=now,
            updated_at=now,
        )


def test_partition_row_rejects_non_positive_dimension():
    """Persisted partition rows reject invalid vector dimensions."""
    now = _now()

    with pytest.raises(ValidationError):
        PartitionRow(name="tenant", dimension=0, created_at=now, updated_at=now)


def test_partition_row_rejects_negative_chat_history_depth():
    """Persisted partition rows reject negative chat history depth."""
    now = _now()

    with pytest.raises(ValidationError):
        PartitionRow(name="tenant", chat_history_depth=-1, created_at=now, updated_at=now)


def test_partition_config_rejects_negative_chat_history_depth():
    """Resolved partition configs reject negative chat history depth."""
    with pytest.raises(ValidationError):
        PartitionConfig(
            name="tenant",
            indexation=IndexationPipelineConfig(),
            retrieval=RetrievalPipelineConfig(),
            chat_history_depth=-1,
        )


def _partition(chat_llm: str | None = None) -> PartitionConfig:
    return PartitionConfig(
        name="tenant",
        indexation=IndexationPipelineConfig(),
        retrieval=RetrievalPipelineConfig(),
        chat_llm=chat_llm,
    )


class TestResolvePartitionChatLlm:
    """resolve_partition_chat_llm — shared by QueryService._resolve_llm (which LLM
    answers) and the chat-completions token preflight (what budget it's checked
    against), so both must agree on the same consensus rule."""

    def test_none_partitions_returns_none(self):
        assert resolve_partition_chat_llm(None, {}) is None

    def test_all_sentinel_returns_none(self):
        assert resolve_partition_chat_llm(["all"], {"a": _partition(chat_llm="mistral")}) is None

    def test_unknown_partition_returns_none(self):
        assert resolve_partition_chat_llm(["missing"], {}) is None

    def test_partition_without_preset_returns_none(self):
        assert resolve_partition_chat_llm(["p"], {"p": _partition()}) is None

    def test_single_partition_preset_resolved(self):
        configs = {"p": _partition(chat_llm="mistral")}
        assert resolve_partition_chat_llm(["p"], configs) == "mistral"

    def test_multi_partition_unanimous_preset_resolved(self):
        configs = {
            "a": _partition(chat_llm="mistral"),
            "b": _partition(chat_llm="mistral"),
            "c": _partition(),  # unset — doesn't veto
        }
        assert resolve_partition_chat_llm(["a", "b", "c"], configs) == "mistral"

    def test_multi_partition_conflicting_presets_returns_none(self):
        configs = {"a": _partition(chat_llm="mistral"), "d": _partition(chat_llm="llama")}
        assert resolve_partition_chat_llm(["a", "d"], configs) is None
