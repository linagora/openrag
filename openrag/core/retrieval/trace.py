"""Request-local collection and safe serialization for retrieval traces."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any

from core.models.chunk import Chunk
from core.models.retrieval_result import ScoredChunk
from core.models.retrieval_trace import (
    REDACTED_ERROR_MESSAGE,
    ContextualizationTrace,
    QueryRetrievalTrace,
    TraceCandidate,
    TraceError,
    TraceStage,
)
from pydantic import BaseModel

TRACE_SCHEMA_VERSION = 1
MAX_TRACE_CANDIDATES_PER_STAGE = 200
MAX_TRACE_CANDIDATES_TOTAL = 5000
MAX_TRACE_QUERY_TRACES_TOTAL = 500
MAX_TRACE_SERIALIZED_BYTES = 4 * 1024 * 1024
MAX_TRACE_QUERY_STRING_BYTES = 4096
MAX_TRACE_IDENTIFIER_BYTES = 1024
MAX_TRACE_STRING_BYTES = 1024
MAX_TRACE_COLLECTION_ITEMS = 500
TRACE_FILE_SCOPE_KIND_KEY = "_trace_file_scope_kind"
TRACE_STAGE_NAMES = (
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
    "file_filter",
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
        "query_traces",
        "errors",
        "configuration_fingerprint",
        "name",
        "status",
        "duration_seconds",
        "candidate_count",
        "candidates",
        "candidate_limit",
        "candidates_truncated",
        "query_trace_limit",
        "query_traces_truncated",
        "trace_truncated",
        "serialized_size_limit",
        "partition",
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
    "candidate": frozenset({"id", "document_id", "partition", "rank", "scores", "duplicate_of", "removal_reason"}),
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
    "comparison": frozenset(
        {
            "status",
            "stages",
            "timings",
            "errors",
            "query_traces",
            "candidate_limit",
            "candidates_truncated",
            "query_trace_limit",
            "query_traces_truncated",
            "trace_truncated",
            "serialized_size_limit",
            "configuration_fingerprint",
        }
    ),
    "query_trace": frozenset({"query", "partition", "attempt", "stages", "timings", "errors", "query_traces"}),
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
    "query_traces": "query_trace",
}
_OMITTED = object()
_REDACTED_CREDENTIAL = "[redacted]"
_AUTH_SCHEME_CREDENTIAL = re.compile(
    r"\b(?P<scheme>bearer|basic)(?P<spacing>\s+)(?P<credential>[^\s,;]+)",
    re.IGNORECASE,
)
_AUTHORIZATION_HEADER_CREDENTIAL = re.compile(
    r"(?P<prefix>\bauthorization\s*:\s*(?:bearer|basic)\s+)(?P<credential>[^\s,;]+)",
    re.IGNORECASE,
)
_QUOTED_NAMED_CREDENTIAL = re.compile(
    r"(?P<prefix>[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|credential|secret|password)[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<credential>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_UNQUOTED_NAMED_CREDENTIAL = re.compile(
    r"(?P<name>\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|credential|secret|password)\b\s*)"
    r"(?P<separator>[:=])(?P<spacing>\s*)"
    r"(?![\"'])(?P<credential>[^\s,;]+)",
    re.IGNORECASE,
)
_JWT_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_COMMON_CREDENTIAL_VALUES = frozenset(
    {
        "admin",
        "changeme",
        "hunter2",
        "letmein",
        "password",
        "passwd",
        "qwerty",
        "secret",
    }
)


class _CandidateBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.truncated = False

    def consume(self) -> bool:
        if self.remaining == 0:
            self.truncated = True
            return False
        self.remaining -= 1
        return True


class _QueryTraceBudget(_CandidateBudget):
    pass


class _SanitizationState:
    def __init__(self) -> None:
        self.collection_truncated = False


class RetrievalDiagnosticsContext:
    """Request-scoped state shared by every trace builder in one retrieval."""

    def __init__(
        self,
        *,
        candidate_limit: int = MAX_TRACE_CANDIDATES_TOTAL,
        query_mode: str = "contextualized",
        compare_original_query: bool = False,
        effective_options: Mapping[str, object] | None = None,
    ) -> None:
        if candidate_limit < 0:
            raise ValueError("candidate_limit must be non-negative")
        self.candidate_limit = candidate_limit
        self.remaining_candidates = candidate_limit
        self.candidates_truncated = False
        self.query_mode = query_mode
        self.compare_original_query = compare_original_query
        self.effective_options = dict(effective_options or {})

    def claim_candidates(self, requested: int) -> int:
        allowed = min(max(requested, 0), self.remaining_candidates)
        self.remaining_candidates -= allowed
        if allowed < requested:
            self.candidates_truncated = True
        return allowed

    def release_candidates(self, count: int) -> None:
        self.remaining_candidates = min(self.candidate_limit, self.remaining_candidates + max(count, 0))


def _looks_like_credential(value: str) -> bool:
    candidate = value.strip()
    if (
        candidate.lower().startswith("or-")
        or candidate.lower() in _COMMON_CREDENTIAL_VALUES
        or _JWT_CREDENTIAL.fullmatch(candidate)
    ):
        return True
    without_sentence_punctuation = candidate.rstrip(".?!")
    if without_sentence_punctuation.isalpha():
        candidate = without_sentence_punctuation
    if (
        len(candidate) >= 6
        and any(character.isalpha() for character in candidate)
        and any(character.isdigit() for character in candidate)
    ):
        return True
    if len(candidate) >= 6 and candidate.isdigit():
        return True
    if len(candidate) >= 8 and any(not character.isalpha() for character in candidate):
        return True
    return len(candidate) >= 20 and candidate.isalnum()


def _scrub_unquoted_named_credential(match: re.Match[str]) -> str:
    name = match.group("name").strip().lower().replace("-", "_")
    credential = match.group("credential")
    strong_secret_name = name not in {"password", "token"}
    if match.group("separator") != "=" and not strong_secret_name and not _looks_like_credential(credential):
        return match.group(0)
    return f"{match.group('name')}{match.group('separator')}{match.group('spacing')}{_REDACTED_CREDENTIAL}"


def _scrub_query_credentials(value: str) -> str:
    """Redact credential-shaped values embedded in public query telemetry."""

    value = _AUTHORIZATION_HEADER_CREDENTIAL.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED_CREDENTIAL}",
        value,
    )
    value = _QUOTED_NAMED_CREDENTIAL.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}{_REDACTED_CREDENTIAL}{match.group('quote')}",
        value,
    )
    value = _UNQUOTED_NAMED_CREDENTIAL.sub(_scrub_unquoted_named_credential, value)
    return _AUTH_SCHEME_CREDENTIAL.sub(
        lambda match: (
            f"{match.group('scheme')}{match.group('spacing')}{_REDACTED_CREDENTIAL}"
            if _looks_like_credential(match.group("credential"))
            else match.group(0)
        ),
        value,
    )


def _child_context(context: str, key: str) -> str:
    if context == "comparisons" and key == "original_query":
        return "comparison"
    return _CHILD_CONTEXTS.get(key, "empty")


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _string_limit(key: str | None) -> int:
    if key in {"original_query", "query"}:
        return MAX_TRACE_QUERY_STRING_BYTES
    if key in {"id", "document_id", "partition", "request_id", "duplicate_of"}:
        return MAX_TRACE_IDENTIFIER_BYTES
    return MAX_TRACE_STRING_BYTES


def _sequence_limit(context: str) -> int | None:
    return {
        "candidate": MAX_TRACE_CANDIDATES_PER_STAGE,
        "stage": len(TRACE_STAGE_NAMES),
        "subquery": 50,
        "temporal_filter": 10,
        "trace_error": 100,
        "query_trace": None,
    }.get(context, MAX_TRACE_COLLECTION_ITEMS)


def _safe_public_value(
    value: object,
    *,
    context: str = "root",
    key: str | None = None,
    candidate_budget: _CandidateBudget,
    query_trace_budget: _QueryTraceBudget,
    state: _SanitizationState,
) -> object:
    candidate_consumed = False
    query_trace_consumed = False
    if isinstance(value, ContextualizationTrace):
        context = "contextualization"
    elif isinstance(value, QueryRetrievalTrace):
        context = "query_trace"
        if not query_trace_budget.consume():
            return _OMITTED
        query_trace_consumed = True
    elif isinstance(value, TraceStage):
        context = "stage"
    elif isinstance(value, TraceCandidate):
        context = "candidate"
        if not candidate_budget.consume():
            return _OMITTED
        candidate_consumed = True
    elif isinstance(value, TraceError):
        context = "trace_error"
    if isinstance(value, BaseModel):
        if isinstance(value, TraceCandidate):
            exclude = {"partition"} if value.partition is None else None
            value = value.model_dump(mode="json", exclude=exclude)
        else:
            value = {field: getattr(value, field) for field in type(value).model_fields}
    if isinstance(value, Mapping):
        if context == "candidate" and not candidate_consumed:
            if not candidate_budget.consume():
                return _OMITTED
        if context == "query_trace" and not query_trace_consumed:
            if not query_trace_budget.consume():
                return _OMITTED
        public: dict[str, object] = {}
        allowed_keys = _PUBLIC_KEYS_BY_CONTEXT[context]
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or raw_key not in allowed_keys:
                continue
            safe_value = _safe_public_value(
                raw_value,
                context=_child_context(context, raw_key),
                key=raw_key,
                candidate_budget=candidate_budget,
                query_trace_budget=query_trace_budget,
                state=state,
            )
            if safe_value is not _OMITTED:
                public[raw_key] = safe_value
        return public
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        public_items: list[object] = []
        sequence_limit = _sequence_limit(context)
        for item in value:
            if sequence_limit is not None and len(public_items) >= sequence_limit:
                state.collection_truncated = True
                break
            if context == "candidate" and candidate_budget.remaining == 0:
                candidate_budget.truncated = True
                break
            if context == "query_trace" and query_trace_budget.remaining == 0:
                query_trace_budget.truncated = True
                break
            safe = _safe_public_value(
                item,
                context=context,
                candidate_budget=candidate_budget,
                query_trace_budget=query_trace_budget,
                state=state,
            )
            if safe is not _OMITTED:
                public_items.append(safe)
        return public_items
    if isinstance(value, Enum):
        return _safe_public_value(
            value.value,
            context=context,
            key=key,
            candidate_budget=candidate_budget,
            query_trace_budget=query_trace_budget,
            state=state,
        )
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return _OMITTED
    if value is None or isinstance(value, (bool, int, float, str)):
        if key in {"error", "message"} and isinstance(value, str):
            return REDACTED_ERROR_MESSAGE
        if key in {"original_query", "query"} and isinstance(value, str):
            value = _scrub_query_credentials(value)
        if isinstance(value, str):
            truncated = _truncate_utf8(value, _string_limit(key))
            if truncated != value:
                state.collection_truncated = True
            return truncated
        return value
    return _OMITTED


def _render_public_value(
    value: object,
    *,
    candidate_limit: int,
    query_trace_limit: int,
    enforce_size_metadata: bool = False,
) -> object:
    candidate_budget = _CandidateBudget(candidate_limit)
    query_trace_budget = _QueryTraceBudget(query_trace_limit)
    state = _SanitizationState()
    safe = _safe_public_value(
        value,
        candidate_budget=candidate_budget,
        query_trace_budget=query_trace_budget,
        state=state,
    )
    if candidate_budget.truncated and isinstance(safe, dict):
        safe["candidate_limit"] = candidate_limit
        safe["candidates_truncated"] = True
    if query_trace_budget.truncated and isinstance(safe, dict):
        safe["query_trace_limit"] = query_trace_limit
        safe["query_traces_truncated"] = True
    trace_truncated = state.collection_truncated or query_trace_budget.truncated or enforce_size_metadata
    if trace_truncated and isinstance(safe, dict):
        safe["trace_truncated"] = True
    if trace_truncated and isinstance(safe, dict):
        safe["serialized_size_limit"] = MAX_TRACE_SERIALIZED_BYTES
    return None if safe is _OMITTED else safe


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def _largest_fitting_limit(render, upper: int) -> tuple[int, object]:
    low = 0
    best_limit = 0
    best = render(0)
    high = upper
    while low <= high:
        midpoint = (low + high) // 2
        candidate = render(midpoint)
        if _serialized_size(candidate) <= MAX_TRACE_SERIALIZED_BYTES:
            best_limit = midpoint
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best_limit, best


def safe_public_value(value: object) -> object:
    """Return a bounded JSON value containing only public allowlisted fields."""
    safe = _render_public_value(
        value,
        candidate_limit=MAX_TRACE_CANDIDATES_TOTAL,
        query_trace_limit=MAX_TRACE_QUERY_TRACES_TOTAL,
    )
    if _serialized_size(safe) <= MAX_TRACE_SERIALIZED_BYTES:
        return safe

    def render_without_candidates(query_trace_limit: int) -> object:
        return _render_public_value(
            value,
            candidate_limit=0,
            query_trace_limit=query_trace_limit,
            enforce_size_metadata=True,
        )

    if _serialized_size(render_without_candidates(MAX_TRACE_QUERY_TRACES_TOTAL)) <= MAX_TRACE_SERIALIZED_BYTES:
        query_trace_limit = MAX_TRACE_QUERY_TRACES_TOTAL
    else:
        query_trace_limit, _ = _largest_fitting_limit(render_without_candidates, MAX_TRACE_QUERY_TRACES_TOTAL)

    _, bounded = _largest_fitting_limit(
        lambda candidate_limit: _render_public_value(
            value,
            candidate_limit=candidate_limit,
            query_trace_limit=query_trace_limit,
            enforce_size_metadata=True,
        ),
        MAX_TRACE_CANDIDATES_TOTAL,
    )
    if _serialized_size(bounded) <= MAX_TRACE_SERIALIZED_BYTES:
        return bounded
    return {
        "trace_truncated": True,
        "serialized_size_limit": MAX_TRACE_SERIALIZED_BYTES,
    }


def canonical_fingerprint(snapshot: Mapping[str, object]) -> str:
    """Return a SHA-256 fingerprint of canonical JSON for *snapshot*."""
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def candidates_from_chunks(chunks: Sequence[Chunk], *, limit: int | None = None) -> list[TraceCandidate]:
    """Project chunks into ordered, content-free trace candidates."""
    candidates: list[TraceCandidate] = []
    visible_chunks = chunks if limit is None else chunks[: max(limit, 0)]
    for rank, chunk in enumerate(visible_chunks, start=1):
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
                partition=chunk.partition or None,
                rank=rank,
                scores=scores,
            )
        )
    return candidates


def merge_child_traces(parent: RetrievalTraceBuilder, children: Sequence[RetrievalTraceBuilder]) -> None:
    """Attach fan-out traces and mark aggregate stages as unavailable."""

    def observed_stage_names(child: RetrievalTraceBuilder | QueryRetrievalTrace) -> set[str]:
        observed = (
            {stage.name for stage in child.stages.values() if stage.status != "not_run"}
            if isinstance(child, RetrievalTraceBuilder)
            else {stage.name for stage in child.stages if stage.status != "not_run"}
        )
        for nested in child.query_traces:
            observed.update(observed_stage_names(nested))
        return observed

    observed_stages: set[str] = set()
    for child in children:
        observed_stages.update(observed_stage_names(child))
        parent.record_query_trace(child)

    for stage_name in observed_stages:
        if parent.stages[stage_name].status == "not_run":
            parent.record_stage(stage_name, status="unavailable", candidates=[])


class RetrievalTraceBuilder:
    """Collect a retrieval trace without changing retrieval return values."""

    def __init__(
        self,
        request_id: str,
        original_query: str | None,
        *,
        partition: str | None = None,
        attempt: str | None = None,
        diagnostics: RetrievalDiagnosticsContext | None = None,
    ) -> None:
        self.request_id = request_id
        self.original_query = original_query
        self.partition = partition
        self.attempt = attempt
        self.diagnostics = diagnostics or RetrievalDiagnosticsContext()
        self.stages = {name: TraceStage(name=name, status="not_run") for name in TRACE_STAGE_NAMES}
        self.contextualization: ContextualizationTrace | None = None
        self.timings: dict[str, float] = {}
        self.comparisons: dict[str, Mapping[str, object]] = {}
        self.query_traces: list[QueryRetrievalTrace] = []
        self.errors: list[TraceError] = []

    @property
    def candidate_capacity(self) -> int:
        return min(MAX_TRACE_CANDIDATES_PER_STAGE, self.diagnostics.remaining_candidates)

    def candidate_capacity_for_stage(self, name: str) -> int:
        previous = self.stages.get(name)
        reusable = len(previous.candidates) if previous is not None else 0
        return min(MAX_TRACE_CANDIDATES_PER_STAGE, self.diagnostics.remaining_candidates + reusable)

    def project_chunks(self, name: str, chunks: Sequence[Chunk]) -> list[TraceCandidate]:
        return candidates_from_chunks(chunks, limit=self.candidate_capacity_for_stage(name))

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
        previous = self.stages.get(name)
        if previous is not None:
            self.diagnostics.release_candidates(len(previous.candidates))
        visible_count = min(len(candidates), MAX_TRACE_CANDIDATES_PER_STAGE)
        retained_count = self.diagnostics.claim_candidates(visible_count)
        self.stages[name] = TraceStage(
            name=name,
            status=status,
            duration_seconds=duration_seconds,
            candidate_count=len(candidates) if candidate_count is None else candidate_count,
            candidates=list(candidates[:retained_count]),
            error=REDACTED_ERROR_MESSAGE if error is not None else None,
        )

    def record_error(self, stage: str, error: Exception | str) -> None:
        kind = type(error).__name__ if isinstance(error, Exception) else "Error"
        self.errors.append(TraceError(stage=stage, message=REDACTED_ERROR_MESSAGE, kind=kind))

    def record_query_trace(self, child: RetrievalTraceBuilder) -> None:
        """Attach one isolated child trace while preserving caller order."""
        self.query_traces.append(
            QueryRetrievalTrace(
                query=child.original_query,
                partition=child.partition,
                attempt=child.attempt,
                stages=list(child.stages.values()),
                timings=dict(child.timings),
                errors=list(child.errors),
                query_traces=list(child.query_traces),
            )
        )

    def finish(self, *, configuration_fingerprint: str) -> dict[str, object]:
        trace: dict[str, Any] = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "request_id": self.request_id,
            "original_query": self.original_query,
            "contextualization": self.contextualization,
            "stages": list(self.stages.values()),
            "timings": self.timings,
            "errors": self.errors,
            "configuration_fingerprint": configuration_fingerprint,
        }
        if self.query_traces:
            trace["query_traces"] = self.query_traces
        trace["comparisons"] = self.comparisons
        if self.diagnostics.candidates_truncated:
            trace["candidate_limit"] = self.diagnostics.candidate_limit
            trace["candidates_truncated"] = True
        public = safe_public_value(trace)
        return public  # type: ignore[return-value]
