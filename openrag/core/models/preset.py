"""Preset and partition domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from pydantic import BaseModel


class PresetRow(BaseModel):
    """DB representation of a pipeline preset row."""

    name: str
    preset_type: str  # "indexation" | "retrieval"
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PartitionRow(BaseModel):
    """DB representation of a partition with preset references."""

    name: str
    display_name: str | None = None
    description: str = ""
    embedder: str = "default"
    indexation_preset: str = "default"
    retrieval_preset: str = "default"
    dimension: int = 1024
    collection_name: str | None = None
    chat_history_depth: int = 0
    chat_llm: str | None = None
    created_at: datetime
    updated_at: datetime


class PartitionConfig(BaseModel):
    """Fully resolved partition config — preset names looked up and validated.

    Built by PartitionService.resolve_partition_row() and cached in
    Settings.partitions at startup and on every preset change.
    """

    name: str
    description: str = ""
    embedder: str = "default"
    indexation: IndexationPipelineConfig
    retrieval: RetrievalPipelineConfig
    collection_name: str | None = None
    chat_history_depth: int = 0
    chat_llm: str | None = None


__all__ = ["PresetRow", "PartitionRow", "PartitionConfig"]
