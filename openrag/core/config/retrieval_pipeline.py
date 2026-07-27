"""Per-partition retrieval pipeline configuration.

Stored as JSONB in the ``pipeline_presets`` table (preset_type='retrieval').
Endpoint name fields (``reranker``, ``llm``) are resolved at query time by
the component factories registered in the DI container.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RetrievalPipelineConfig(BaseModel):
    """Retrieval pipeline settings for one partition preset."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["single", "multiQuery", "hyde"] = "single"
    reranker: str | None = None  # endpoint name; None = use default
    llm: str | None = None  # endpoint name for multiQuery / hyde expansion

    top_k: int = Field(default=50, gt=0, le=1000)  # vector candidates fetched
    top_n: int = Field(default=10, gt=0, le=1000)  # final results after reranking
    enable_reranker: bool = True
    similarity_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    include_related: bool = True
    include_ancestors: bool = True
    rrf_k: int = Field(default=60, gt=0, le=1000)  # Reciprocal Rank Fusion constant

    # Prompt selection: name a library prompt for this preset's query-expansion
    # strategies (None = the type's global default, then the disk seed).
    # Resolved per request in RetrievalService.resolve_prompt.
    hyde_prompt_name: str | None = None
    multi_query_prompt_name: str | None = None


__all__ = ["RetrievalPipelineConfig"]
