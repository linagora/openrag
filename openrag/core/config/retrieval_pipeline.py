"""Per-partition retrieval pipeline configuration.

Stored as JSONB in the ``pipeline_presets`` table (preset_type='retrieval').
The ``reranker`` endpoint name is resolved at query time by the component
factories registered in the DI container.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RetrievalPipelineConfig(BaseModel):
    """Retrieval pipeline settings for one partition preset."""

    # Stored presets may still carry REMOVED_FIELDS; they are ignored on load.
    model_config = ConfigDict(extra="ignore")

    reranker: str | None = None  # endpoint name; None = use default

    top_k: int = Field(default=50, gt=0, le=1000)  # vector candidates fetched
    # Final results after reranking: the chunks the LLM gets to answer.
    # None follows the deployment's reranker.top_k (RERANKER_TOP_K).
    top_n: int | None = Field(default=None, gt=0, le=1000)
    enable_reranker: bool = True
    similarity_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    include_related: bool = True
    include_ancestors: bool = True
    rrf_k: int = Field(default=60, gt=0, le=1000)  # Reciprocal Rank Fusion constant

    # The library prompt that rewrites the user's query before retrieval
    # (None = the type's global default, then the disk seed), resolved in
    # QueryContextualizer.contextualize. A query-side concern, so it lives on the
    # retrieval preset rather than with the partition's generation prompts.
    query_contextualizer_prompt_name: str | None = None

    def effective_top_n(self, default: int) -> int:
        """``top_n``, or *default* (the global ``reranker.top_k``) when the preset leaves it unset."""
        return self.top_n if self.top_n is not None else default


#: Fields of the removed ``multiQuery`` and ``hyde`` retrievers. Stored presets
#: still holding them load as before (the fields are ignored); a preset written
#: through the API is stored without them.
REMOVED_FIELDS = frozenset({"type", "llm", "hyde_prompt_name", "multi_query_prompt_name"})


__all__ = ["REMOVED_FIELDS", "RetrievalPipelineConfig"]
