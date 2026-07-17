"""Per-partition indexation pipeline configuration.

Stored as JSONB in the ``pipeline_presets`` table (preset_type='indexation').
At runtime, PartitionService deserializes each preset row into this model
and caches the resolved PartitionConfig.
"""

from __future__ import annotations

from typing import Literal

from core.config.chunking import ChunkerConfig
from pydantic import BaseModel, ConfigDict, Field

# PDF parsing backends a preset may explicitly select. ``None`` (the default)
# means "inherit the deployment's global PDFLOADER" — a preset that doesn't care
# about PDF parsing follows the operator's global ``file_loaders.pdf`` choice
# instead of silently forcing one backend (and, with it, lazily spinning up that
# backend's Ray pool). See pipeline_builder._select_parser.
PARSING_STRATEGIES: tuple[str, ...] = ("pymupdf", "marker", "docling")


class IndexationPipelineConfig(BaseModel):
    """Indexation pipeline settings for one partition preset."""

    model_config = ConfigDict(extra="ignore")

    chunking: ChunkerConfig = Field(default_factory=ChunkerConfig)
    # None => inherit the global PDFLOADER (see PARSING_STRATEGIES above).
    parsing_strategy: Literal["pymupdf", "marker", "docling"] | None = None

    # VLM / image captioning
    vlm: str | None = None  # endpoint name; None = use global default
    enable_image_captioning: bool = True

    # Contextualization (LLM-generated chunk context)
    enable_contextualization: bool = False
    contextualization_llm: str | None = None
    contextualization_mode: Literal["none", "simple", "structured"] = "none"

    # Metadata extraction
    enable_metadata_extraction: bool = True
    metadata_extraction_llm: str | None = None

    # Prompt name overrides (None = use active prompt for the partition)
    vlm_caption_prompt_name: str | None = None
    contextualization_prompt_name: str | None = None

    # Entity extraction
    enable_entity_extraction: bool = True
    entity_labels: list[str] = Field(
        default_factory=lambda: ["person", "organization", "location", "event"],
    )

    # Topic tagging. Off by default: the generated tags are not yet surfaced in
    # the API or used for retrieval, so enabling it only buys an extra LLM call
    # per document. Partitions that want the tags opt in on their preset.
    enable_topic_tagging: bool = False
    max_topic_tags: int = Field(default=7, ge=1, le=50)
    topic_tagging_llm: str | None = None


__all__ = ["IndexationPipelineConfig", "PARSING_STRATEGIES"]
