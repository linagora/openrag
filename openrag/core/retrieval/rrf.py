"""Reciprocal Rank Fusion — pure math, no domain coupling.

Combines multiple ranked lists into a single ranking by summing reciprocal
ranks across lists. Items present in more lists, or higher-ranked in any
list, sort to the top of the fused result.

Formula:
    score(item) = Σ_i 1 / (k + rank_i)

with ``rank_i`` the 1-based rank of the item in list ``i``. Smaller ``k``
amplifies the top of each list; ``k=60`` is the canonical default and
balances rank sensitivity across lists.

Identification of "the same item" is delegated to the caller via
``key_fn`` — typically returning the chunk id, document id, or URL.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from typing import TYPE_CHECKING, TypeVar

from core.models.retrieval_trace import TraceCandidate, TraceRemovalReason, TraceStageName

if TYPE_CHECKING:
    from core.retrieval.trace import RetrievalTraceBuilder

T = TypeVar("T")


def rrf_reranking(
    ranked_lists: Sequence[Sequence[T]],
    key_fn: Callable[[T], Hashable] | None = None,
    k: int = 60,
    top_k: int | None = None,
    trace: RetrievalTraceBuilder | None = None,
    trace_stage: TraceStageName = "hybrid_fused",
) -> list[T]:
    """Fuse multiple ranked lists into one via Reciprocal Rank Fusion.

    Args:
        ranked_lists: Each inner sequence is a ranked list (best first).
        key_fn: Returns the identity key for an item; items sharing a key
                across lists have their RRF scores summed. Defaults to
                ``id(item)`` (object identity), which prevents fusion across
                lists for items lacking a logical id.
        k: RRF dampening constant. ``60`` is canonical.

    Returns:
        A single ranked list, best first. Empty input -> empty list.
        Single input list is shallow-copied so callers always get a ``list``.

    Raises:
        ValueError: if ``k < 0`` (would produce a zero or negative
        denominator at rank 1 or below and crash with ZeroDivisionError).
    """
    if k < 0:
        raise ValueError(f"RRF k must be non-negative, got {k}")
    if not ranked_lists:
        _record_rrf_trace(trace, [], {}, {}, top_k, trace_stage)
        return []
    if len(ranked_lists) == 1:
        result = list(ranked_lists[0])
        _record_single_list_trace(trace, result, key_fn, k, top_k, trace_stage)
        return result[:top_k] if top_k is not None else result

    if key_fn is None:
        key_fn = id  # type: ignore[assignment]

    fused: dict[Hashable, tuple[float, T]] = {}
    occurrences: dict[Hashable, list[T]] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            key = key_fn(item)
            occurrences.setdefault(key, []).append(item)
            score, kept = fused.get(key, (0.0, item))
            fused[key] = (score + 1.0 / (rank + k), kept)

    ordered = sorted(fused.items(), key=lambda entry: entry[1][0], reverse=True)
    result = [item for _, (_, item) in ordered]
    _record_rrf_trace(trace, result, {key: score for key, (score, _) in fused.items()}, occurrences, top_k, trace_stage)
    return result[:top_k] if top_k is not None else result


def _record_rrf_trace(
    trace: RetrievalTraceBuilder | None,
    ordered: Sequence[T],
    scores: dict[Hashable, float],
    occurrences: dict[Hashable, list[T]],
    top_k: int | None,
    trace_stage: TraceStageName,
) -> None:
    """Record fused membership without letting optional telemetry affect RRF."""
    if trace is None:
        return
    try:
        candidates: list[TraceCandidate] = []
        key_for_object: dict[int, Hashable] = {id(item): key for key, items in occurrences.items() for item in items}
        for rank, item in enumerate(ordered, start=1):
            key = key_for_object.get(id(item), id(item))
            removal = None
            if top_k is not None and rank > top_k:
                removal = TraceRemovalReason(
                    code="final_top_n",
                    explanation="Excluded by the final public result cutoff.",
                )
            candidate = TraceCandidate(
                id=str(getattr(item, "id", key)),
                document_id=getattr(item, "document_id", None) or None,
                rank=rank,
                scores={"fused": scores.get(key, 0.0)},
                removal_reason=removal,
            )
            candidates.append(candidate)
            for duplicate in occurrences.get(key, [])[1:]:
                candidates.append(
                    TraceCandidate(
                        id=str(getattr(duplicate, "id", key)),
                        document_id=getattr(duplicate, "document_id", None) or None,
                        rank=rank,
                        scores={"fused": scores.get(key, 0.0)},
                        duplicate_of=str(getattr(item, "id", key)),
                        removal_reason=TraceRemovalReason(
                            code="duplicate",
                            explanation="Duplicate identity merged during reciprocal-rank fusion.",
                        ),
                    )
                )
        trace.record_stage(trace_stage, status="complete", candidates=candidates)
    except Exception as error:
        try:
            trace.record_error(trace_stage, error)
        except Exception:
            pass


def _record_single_list_trace(
    trace: RetrievalTraceBuilder | None,
    ordered: Sequence[T],
    key_fn: Callable[[T], Hashable] | None,
    k: int,
    top_k: int | None,
    trace_stage: TraceStageName,
) -> None:
    """Trace a pass-through list while retaining duplicate occurrences once."""
    if trace is None:
        return
    try:
        keys = [key_fn(item) if key_fn is not None else id(item) for item in ordered]
        scores: dict[Hashable, float] = {}
        for rank, key in enumerate(keys, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (rank + k)

        first_ids: dict[Hashable, str] = {}
        candidates: list[TraceCandidate] = []
        for rank, (key, item) in enumerate(zip(keys, ordered, strict=True), start=1):
            candidate_id = str(getattr(item, "id", key))
            duplicate_of = first_ids.get(key)
            removal = None
            if duplicate_of is not None:
                removal = TraceRemovalReason(
                    code="duplicate",
                    explanation="Duplicate identity retained in the source ranking.",
                )
            elif top_k is not None and rank > top_k:
                removal = TraceRemovalReason(
                    code="final_top_n",
                    explanation="Excluded by the final public result cutoff.",
                )
            first_ids.setdefault(key, candidate_id)
            candidates.append(
                TraceCandidate(
                    id=candidate_id,
                    document_id=getattr(item, "document_id", None) or None,
                    rank=rank,
                    scores={"fused": scores[key]},
                    duplicate_of=duplicate_of,
                    removal_reason=removal,
                )
            )
        trace.record_stage(trace_stage, status="complete", candidates=candidates)
    except Exception as error:
        try:
            trace.record_error(trace_stage, error)
        except Exception:
            pass
