"""Retrieval query domain model."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RetrievalQuery(BaseModel):
    """A user query with retrieval parameters."""

    text: str
    partition: str = "default"
    top_k: int = 10
    similarity_threshold: float = 0.95
    filters: dict[str, Any] = Field(default_factory=dict)
    include_related: bool = False
    include_ancestors: bool = False
    related_limit: int = 10
    max_ancestor_depth: int | None = None
    with_surrounding_chunks: bool = True
    rerank: bool = True
