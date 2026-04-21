"""Retrieval response — end-to-end retrieval output."""

from __future__ import annotations

from pydantic import BaseModel, Field

from openrag.core.models.retrieval_result import RetrievalResult


class RetrievalResponse(BaseModel):
    """Complete response from a retrieval pipeline execution."""

    query: str = ""
    results: list[RetrievalResult] = Field(default_factory=list)
    pipeline_used: str | None = None
    partition: str = "default"
    total_candidates: int = 0
    latency_ms: float | None = None
