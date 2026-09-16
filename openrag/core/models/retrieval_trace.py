"""Typed public contract for retrieval trace schema version 1."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TraceStageName = Literal[
    "original_query",
    "contextualized_query",
    "dense_before_threshold",
    "dense_after_threshold",
    "sparse",
    "hybrid_fused",
    "pre_rerank",
    "post_rerank",
    "final",
]
TraceStatus = Literal["complete", "not_run", "unavailable", "error"]
TraceScoreName = Literal["dense", "sparse", "fused", "reranker"]
RemovalReasonCode = Literal[
    "dense_threshold",
    "hybrid_top_k",
    "reranker_top_n",
    "final_top_n",
    "duplicate",
    "partition_filter",
    "workspace_filter",
    "attachment_filter",
    "temporal_filter",
]


class _TraceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TraceRemovalReason(_TraceModel):
    """Why a candidate did not advance to the next retrieval stage."""

    code: RemovalReasonCode
    explanation: str = Field(max_length=500)


class TraceCandidate(_TraceModel):
    """Public, content-free candidate telemetry for one retrieval stage."""

    id: str
    document_id: str | None = None
    rank: int = Field(ge=1)
    scores: dict[TraceScoreName, float] = Field(default_factory=dict)
    duplicate_of: str | None = None
    removal_reason: TraceRemovalReason | None = None


class TraceStage(_TraceModel):
    """Ordered candidate state at one stable retrieval boundary."""

    name: TraceStageName
    status: TraceStatus
    duration_seconds: float | None = Field(default=None, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    candidates: list[TraceCandidate] = Field(default_factory=list)
    error: str | None = Field(default=None, max_length=500)


class TraceError(_TraceModel):
    """A bounded error that made retrieval telemetry partial."""

    stage: str
    message: str = Field(max_length=500)
    kind: str | None = None


class ContextualizationTrace(_TraceModel):
    """Public query-contextualization decisions attached by chat tracing."""

    original_query: str | None = None
    subqueries: list[dict[str, object]] = Field(default_factory=list)
    intent: str | None = None
    requires_retrieval: bool | None = None
    fallback_used: bool = False
    error: TraceError | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    endpoint: str | None = None
    model: str | None = None
    prompt: dict[str, object] | None = None
