"""Preset and partition domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
from pydantic import BaseModel, Field

PresetType = Literal["indexation", "retrieval"]


class PresetRow(BaseModel):
    """DB representation of a pipeline preset row."""

    name: str
    preset_type: PresetType
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
    dimension: int = Field(default=1024, gt=0)
    collection_name: str | None = None
    chat_history_depth: int = Field(default=0, ge=0)
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
    chat_history_depth: int = Field(default=0, ge=0)
    chat_llm: str | None = None


def resolve_partition_chat_llm(
    partitions: list[str] | None,
    partition_configs: dict[str, PartitionConfig],
) -> str | None:
    """Resolve the ``chat_llm`` preset every partition in *partitions* agrees on.

    ``None`` means "no single owning preset — the caller should fall back to
    the default LLM": a direct-LLM request (no partitions), the ``"all"``
    cross-partition sentinel, or a multi-partition request whose partitions
    disagree (or none set a preset). Shared by the answering-LLM resolution
    (``QueryService._resolve_llm``) and the chat-completions token preflight
    (``api.routers.user.chat``), so the LLM that actually answers a request
    and the budget it was checked against never fall out of sync.
    """
    if not partitions or "all" in partitions:
        return None
    names = {cfg.chat_llm for name in partitions if (cfg := partition_configs.get(name)) is not None and cfg.chat_llm}
    return names.pop() if len(names) == 1 else None


__all__ = ["PresetRow", "PartitionRow", "PartitionConfig", "PresetType", "resolve_partition_chat_llm"]
