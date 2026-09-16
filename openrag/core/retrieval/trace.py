"""Request-local collection and safe serialization for retrieval traces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.models.chunk import Chunk
from core.models.retrieval_result import ScoredChunk
from core.models.retrieval_trace import (
    ContextualizationTrace,
    TraceCandidate,
    TraceError,
    TraceStage,
)
from pydantic import BaseModel

TRACE_SCHEMA_VERSION = 1
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

_PUBLIC_KEYS = frozenset(
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
        "endpoint",
        "model",
        "prompt",
        "source",
        "content_hash",
        "dense",
        "sparse",
        "fused",
        "reranker",
        "original_query",
        "embedding",
        "dense_search",
        "sparse_search",
        "fusion",
        "reranking",
        "total",
    }
).union(TRACE_STAGE_NAMES)
_OMITTED = object()


def _public_endpoint(value: str) -> str:
    """Remove URL credentials, query parameters, and fragments."""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    hostname = parts.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parts.scheme, f"{hostname}{port}", parts.path, "", ""))


def _safe_public_value(value: object, *, key: str | None = None) -> object:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        public: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or raw_key not in _PUBLIC_KEYS:
                continue
            safe_value = _safe_public_value(raw_value, key=raw_key)
            if safe_value is not _OMITTED:
                public[raw_key] = safe_value
        return public
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [safe for item in value if (safe := _safe_public_value(item)) is not _OMITTED]
    if isinstance(value, Enum):
        return _safe_public_value(value.value, key=key)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, float, str)):
        if key == "endpoint" and isinstance(value, str):
            return _public_endpoint(value)
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
        duration_seconds: float | None = None,
        error: str | None = None,
    ) -> None:
        self.stages[name] = TraceStage(
            name=name,
            status=status,
            duration_seconds=duration_seconds,
            candidate_count=len(candidates),
            candidates=list(candidates),
            error=error[:500] if error is not None else None,
        )

    def record_error(self, stage: str, error: Exception | str) -> None:
        self.errors.append(TraceError(stage=stage, message=str(error)[:500]))

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
