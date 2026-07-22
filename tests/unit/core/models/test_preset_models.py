"""Tests for persisted preset and partition domain row invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from core.models.preset import PartitionConfig, PartitionRow, PresetRow
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


def test_partition_row_rejects_zero_chat_history_depth():
    """Persisted partition rows reject zero chat history depth (minimum is 1)."""
    now = _now()

    with pytest.raises(ValidationError):
        PartitionRow(name="tenant", chat_history_depth=0, created_at=now, updated_at=now)


def test_partition_config_rejects_zero_chat_history_depth():
    """Resolved partition configs reject zero chat history depth (minimum is 1)."""
    with pytest.raises(ValidationError):
        PartitionConfig(
            name="tenant",
            indexation=IndexationPipelineConfig(),
            retrieval=RetrievalPipelineConfig(),
            chat_history_depth=0,
        )
