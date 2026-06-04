"""Per-partition indexation pipeline configuration.

Stored as JSONB in the ``pipeline_presets`` table (preset_type='indexation').
At runtime, PartitionService deserializes each preset row into this model
and caches the resolved PartitionConfig.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .chunking import ChunkerConfig


class IndexationPipelineConfig(BaseModel):
    """Indexation pipeline settings for one partition preset."""

    model_config = ConfigDict(extra="ignore")

    chunking: ChunkerConfig = Field(default_factory=ChunkerConfig)
    parsing_strategy: str = "marker"  # "pymupdf" | "marker" | "docling"

    # VLM / image captioning
    vlm: str | None = None  # endpoint name; None = use global default
    enable_image_captioning: bool = True

    # Contextualization (LLM-generated chunk context)
    enable_contextualization: bool = False
    contextualization_llm: str | None = None
    contextualization_mode: str = "none"  # "none" | "simple" | "structured"

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

    # Topic tagging
    enable_topic_tagging: bool = True
    max_topic_tags: int = Field(default=7, ge=1, le=50)
    topic_tagging_llm: str | None = None


__all__ = ["IndexationPipelineConfig"]
