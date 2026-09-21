"""Request-local collection and safe serialization for retrieval traces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

from core.models.chunk import Chunk
from core.models.retrieval_result import ScoredChunk
from core.models.retrieval_trace import (
    REDACTED_ERROR_MESSAGE,
    ContextualizationTrace,
    TraceCandidate,
    TraceError,
    TraceStage,
)
from pydantic import BaseModel

TRACE_SCHEMA_VERSION = 1
MAX_TRACE_CANDIDATES_PER_STAGE = 200
TRACE_STAGE_NAMES = (
    "original_query",
    "contextualized_query",
    "dense_before_threshold",
    "dense_after_threshold",
    "sparse",
    "hybrid_fused",
    "pre_rerank",
    "post_rerank",
    "final",
)
TRACE_STATUSES = ("complete", "not_run", "unavailable", "error")
REMOVAL_REASONS = (
    "dense_threshold",
    "hybrid_top_k",
    "reranker_top_n",
    "final_top_n",
    "duplicate",
    "partition_filter",
    "workspace_filter",
    "attachment_filter",
    "temporal_filter",
)

_ROOT_PUBLIC_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "original_query",
        "contextualization",
        "stages",
        "timings",
        "comparisons",
        "errors",
        "configuration_fingerprint",
        "name",
        "status",
        "duration_seconds",
        "candidate_count",
        "candidates",
        "error",
        "id",
        "document_id",
        "rank",
        "scores",
        "duplicate_of",
        "removal_reason",
        "code",
        "explanation",
        "stage",
        "message",
        "kind",
        "subqueries",
        "query",
        "temporal_filters",
        "operator",
        "value",
        "intent",
        "requires_retrieval",
        "fallback_used",
        "bypassed",
        "model",
        "prompt",
        "source",
        "content_hash",
        "dense",
        "sparse",
        "fused",
        "reranker",
        "original_query",
        "dense_search",
        "sparse_search",
        "fusion",
        "reranking",
        "total",
    }
).union(TRACE_STAGE_NAMES)
_PUBLIC_KEYS_BY_CONTEXT = {
    "root": _ROOT_PUBLIC_KEYS,
    "contextualization": frozenset(
        {
            "original_query",
            "subqueries",
            "intent",
            "requires_retrieval",
            "fallback_used",
            "bypassed",
            "error",
            "duration_seconds",
            "model",
            "prompt",
        }
    ),
    "prompt": frozenset({"content_hash", "name", "source"}),
    "subquery": frozenset({"query", "temporal_filters"}),
    "temporal_filter": frozenset({"operator", "value"}),
    "stage": frozenset({"name", "status", "duration_seconds", "candidate_count", "candidates", "error"}),
    "candidate": frozenset({"id", "document_id", "rank", "scores", "duplicate_of", "removal_reason"}),
    "scores": frozenset({"dense", "sparse", "fused", "reranker"}),
    "removal_reason": frozenset({"code", "explanation"}),
    "trace_error": frozenset({"stage", "message", "kind"}),
    "timings": frozenset(
        {
            "contextualization",
            "embedding",
            "dense_search",
            "sparse_search",
            "fusion",
            "reranking",
            "total",
        }
    ).union(TRACE_STAGE_NAMES),
    "comparisons": frozenset({"original_query"}),
    "comparison": frozenset({"status", "stages", "timings", "errors", "configuration_fingerprint"}),
    "empty": frozenset(),
}
_CHILD_CONTEXTS = {
    "contextualization": "contextualization",
    "prompt": "prompt",
    "subqueries": "subquery",
    "temporal_filters": "temporal_filter",
    "stages": "stage",
    "candidates": "candidate",
    "scores": "scores",
    "removal_reason": "removal_reason",
    "errors": "trace_error",
    "error": "trace_error",
    "timings": "timings",
    "comparisons": "comparisons",
}
_OMITTED = object()


def _child_context(context: str, key: str) -> str:
    if context == "comparisons" and key == "original_query":
        return "comparison"
    return _CHILD_CONTEXTS.get(key, "empty")


def _safe_public_value(value: object, *, context: str = "root", key: str | None = None) -> object:
    if isinstance(value, ContextualizationTrace):
        context = "contextualization"
    elif isinstance(value, TraceStage):
        context = "stage"
    elif isinstance(value, TraceCandidate):
        context = "candidate"
    elif isinstance(value, TraceError):
        context = "trace_error"
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        public: dict[str, object] = {}
        allowed_keys = _PUBLIC_KEYS_BY_CONTEXT[context]
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or raw_key not in allowed_keys:
                continue
            safe_value = _safe_public_value(
                raw_value,
                context=_child_context(context, raw_key),
                key=raw_key,
            )
            if safe_value is not _OMITTED:
                public[raw_key] = safe_value
        return public
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [safe for item in value if (safe := _safe_public_value(item, context=context)) is not _OMITTED]
    if isinstance(value, Enum):
        return _safe_public_value(value.value, context=context, key=key)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        if key in {"error", "message"} and isinstance(value, str):
            return REDACTED_ERROR_MESSAGE
        return value
    return _OMITTED


def safe_public_value(value: object) -> object:
    """Return a JSON-compatible value containing only public allowlisted fields."""
    safe = _safe_public_value(value)
    return None if safe is _OMITTED else safe


def canonical_fingerprint(snapshot: Mapping[str, object]) -> str:
    """Return a SHA-256 fingerprint of canonical JSON for *snapshot*."""
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def candidates_from_chunks(chunks: Sequence[Chunk]) -> list[TraceCandidate]:
    """Project chunks into ordered, content-free trace candidates."""
    candidates: list[TraceCandidate] = []
    for rank, chunk in enumerate(chunks, start=1):
        scores: dict[str, float] = {}
        if isinstance(chunk, ScoredChunk):
            if chunk.vector_score is not None:
                scores["dense"] = chunk.vector_score
            if chunk.combined_score is not None:
                scores["fused"] = chunk.combined_score
            if chunk.rerank_score is not None:
                scores["reranker"] = chunk.rerank_score
        candidates.append(
            TraceCandidate(
                id=chunk.id,
                document_id=chunk.document_id or None,
                rank=rank,
                scores=scores,
            )
        )
    return candidates


class RetrievalTraceBuilder:
    """Collect a retrieval trace without changing retrieval return values."""

    def __init__(self, request_id: str, original_query: str) -> None:
        self.request_id = request_id
        self.original_query = original_query
        self.stages = {name: TraceStage(name=name, status="not_run") for name in TRACE_STAGE_NAMES}
        self.contextualization: ContextualizationTrace | None = None
        self.timings: dict[str, float] = {}
        self.comparisons: dict[str, Mapping[str, object]] = {}
        self.errors: list[TraceError] = []

    def record_stage(
        self,
        name: str,
        *,
        status: str,
        candidates: Sequence[TraceCandidate],
        candidate_count: int | None = None,
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> None:
        self.stages[name] = TraceStage(
            name=name,
            status=status,
            duration_seconds=duration_seconds,
            candidate_count=len(candidates) if candidate_count is None else candidate_count,
            candidates=list(candidates[:MAX_TRACE_CANDIDATES_PER_STAGE]),
            error=REDACTED_ERROR_MESSAGE if error is not None else None,
        )

    def record_error(self, stage: str, error: Exception | str) -> None:
        kind = type(error).__name__ if isinstance(error, Exception) else "Error"
        self.errors.append(TraceError(stage=stage, message=REDACTED_ERROR_MESSAGE, kind=kind))

    def finish(self, *, configuration_fingerprint: str) -> dict[str, object]:
        trace: dict[str, Any] = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "request_id": self.request_id,
            "original_query": self.original_query,
            "contextualization": self.contextualization,
            "stages": list(self.stages.values()),
            "timings": self.timings,
            "comparisons": self.comparisons,
            "errors": self.errors,
            "configuration_fingerprint": configuration_fingerprint,
        }
        return safe_public_value(trace)  # type: ignore[return-value]
