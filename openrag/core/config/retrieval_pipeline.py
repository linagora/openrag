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

    # Context compression. Applied to retrieved chunk text (and optionally the
    # chat history) before the prompt is assembled. Requires the deployment to
    # have compression enabled; the backend is deployment-wide.
    compression_enabled: bool = False
    compression_target_ratio: float | None = Field(default=None, gt=0.0, le=1.0)
    compress_history: bool = False
    compress_history_keep_recent: int = Field(default=2, ge=0, le=50)

    # Prompt selection: name a library prompt for this preset's query-side
    # prompts (None = the type's global default, then the disk seed). hyde /
    # multi_query drive the query-expansion strategies (resolved per request in
    # RetrievalService); query_contextualizer rewrites the user's query before
    # retrieval (resolved in QueryService.generate_query). All three are
    # query-side concerns, so they live on the retrieval preset rather than the
    # partition's generation prompts.
    hyde_prompt_name: str | None = None
    multi_query_prompt_name: str | None = None
    query_contextualizer_prompt_name: str | None = None


__all__ = ["RetrievalPipelineConfig"]
