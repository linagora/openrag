"""Retrieval result domain models — per-chunk scored results."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RetrievalResult(BaseModel):
    """A single scored chunk from retrieval."""

    chunk_id: str = ""
    document_id: str = ""
    text: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    rerank_score: float | None = None
    page_number: int | None = None


class ScoredChunk(BaseModel):
    """A chunk with both vector and rerank scores."""

    chunk_id: str = ""
    document_id: str = ""
    text: str = ""
    vector_score: float = 0.0
    rerank_score: float | None = None
    combined_score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    page_number: int | None = None
