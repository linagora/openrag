"""Typed public contract for retrieval trace schema version 1."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

REDACTED_ERROR_MESSAGE = "redacted"

TraceStageName = Literal[
    "original_query",
    "contextualized_query",
    "dense_before_threshold",
    "dense_after_threshold",
    "sparse",
    "hybrid_fused",
    "multi_query_fused",
    "partition_fused",
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

    @field_validator("error", mode="before")
    @classmethod
    def redact_error(cls, value: object) -> str | None:
        return None if value is None else REDACTED_ERROR_MESSAGE


class TraceError(_TraceModel):
    """A bounded error that made retrieval telemetry partial."""

    stage: str
    message: str = Field(max_length=500)
    kind: str | None = None

    @field_validator("message", mode="before")
    @classmethod
    def redact_message(cls, value: object) -> str:
        return REDACTED_ERROR_MESSAGE


class QueryRetrievalTrace(_TraceModel):
    """Content-free retrieval stages produced for one partition or query."""

    query: str | None = None
    partition: str | None = None
    stages: list[TraceStage] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    errors: list[TraceError] = Field(default_factory=list)
    query_traces: list[QueryRetrievalTrace] = Field(default_factory=list)


class TemporalFilterTrace(_TraceModel):
    """Public temporal predicate generated during contextualization."""

    operator: str
    value: str


class ContextualizedSubqueryTrace(_TraceModel):
    """One generated query and its public temporal predicates."""

    query: str
    temporal_filters: list[TemporalFilterTrace] = Field(default_factory=list)


class PromptTrace(_TraceModel):
    """Public prompt identity without prompt content."""

    content_hash: str
    name: str | None = None
    source: str | None = None


class ContextualizationTrace(_TraceModel):
    """Public query-contextualization decisions attached by chat tracing."""

    original_query: str | None = None
    subqueries: list[ContextualizedSubqueryTrace] = Field(default_factory=list)
    intent: str | None = None
    requires_retrieval: bool | None = None
    fallback_used: bool = False
    bypassed: bool = False
    error: TraceError | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    model: str | None = None
    prompt: PromptTrace | None = None
