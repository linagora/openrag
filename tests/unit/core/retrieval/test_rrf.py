"""RRF unit tests — fusion semantics and edge cases."""

from __future__ import annotations

import core.retrieval.rrf as rrf_module
import pytest
from core.models.chunk import Chunk
from core.models.retrieval_trace import TraceCandidate
from core.retrieval.rrf import rrf_reranking
from core.retrieval.trace import RetrievalDiagnosticsContext, RetrievalTraceBuilder


def test_rrf_empty_returns_empty():
    assert rrf_reranking([]) == []


def test_rrf_single_list_returned_as_is():
    items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert rrf_reranking([items]) == items


def test_rrf_fuses_overlapping_results():
    # 'a' is rank 1 in list1 and rank 2 in list2 → top
    # 'c' is rank 1 in list2 → second
    # 'b' is rank 2 in list1 only
    list1 = [{"id": "a"}, {"id": "b"}]
    list2 = [{"id": "c"}, {"id": "a"}]
    fused = rrf_reranking([list1, list2], key_fn=lambda x: x["id"])
    ids = [item["id"] for item in fused]
    assert ids[0] == "a"
    assert set(ids) == {"a", "b", "c"}


def test_rrf_without_key_fn_does_not_fuse():
    list1 = [{"id": "a"}]
    list2 = [{"id": "a"}]  # different object, same logical id
    fused = rrf_reranking([list1, list2])
    # Object identity → two separate items in fused result
    assert len(fused) == 2


def test_rrf_smaller_k_emphasizes_top_ranks():
    list1 = [{"id": "a"}, {"id": "b"}]
    list2 = [{"id": "b"}, {"id": "a"}]
    fused = rrf_reranking([list1, list2], key_fn=lambda x: x["id"], k=1)
    # k=1: top-rank in any list dominates; with two top-1s for different items,
    # both score the same — order is stable across implementations though
    assert {item["id"] for item in fused} == {"a", "b"}


def test_rrf_rejects_negative_k():
    with pytest.raises(ValueError, match="non-negative"):
        rrf_reranking([[{"id": "a"}], [{"id": "b"}]], key_fn=lambda x: x["id"], k=-1)


def test_rrf_trace_retains_duplicate_membership_and_fused_score():
    first_a = Chunk(id="a", text="first-a", partition="legal")
    duplicate_a = Chunk(id="a", text="duplicate-a", partition="science")
    b = Chunk(id="b", text="b")
    c = Chunk(id="c", text="c")
    trace = RetrievalTraceBuilder("req-1", "q")

    fused = rrf_reranking(
        [[first_a, b], [c, duplicate_a]],
        key_fn=lambda chunk: chunk.id,
        trace=trace,
    )

    assert fused == [first_a, c, b]
    candidates = trace.stages["hybrid_fused"].candidates
    assert [(candidate.id, candidate.rank) for candidate in candidates] == [
        ("a", 1),
        ("a", 1),
        ("c", 2),
        ("b", 3),
    ]
    duplicate = next(candidate for candidate in candidates if candidate.duplicate_of is not None)
    assert candidates[0].partition == "legal"
    assert duplicate.partition == "science"
    assert duplicate.duplicate_of == "a"
    assert duplicate.removal_reason.code == "duplicate"
    assert duplicate.scores["fused"] == pytest.approx(1 / 61 + 1 / 62)


def test_rrf_trace_marks_public_cutoff_without_changing_fused_objects():
    a, b, c = (Chunk(id=cid, text=cid) for cid in ("a", "b", "c"))
    trace = RetrievalTraceBuilder("req-1", "q")

    fused = rrf_reranking(
        [[a, b], [b, c]],
        key_fn=lambda chunk: chunk.id,
        top_k=2,
        trace=trace,
    )

    assert fused == [b, a]
    canonical_c = next(
        candidate
        for candidate in trace.stages["hybrid_fused"].candidates
        if candidate.id == "c" and candidate.duplicate_of is None
    )
    assert canonical_c.removal_reason.code == "final_top_n"


def test_rrf_single_list_trace_records_each_duplicate_once():
    first_a = Chunk(id="a", text="first-a")
    duplicate_a = Chunk(id="a", text="duplicate-a")
    trace = RetrievalTraceBuilder("req-1", "q")

    result = rrf_reranking(
        [[first_a, duplicate_a]],
        key_fn=lambda chunk: chunk.id,
        trace=trace,
    )

    assert result == [first_a, duplicate_a]
    candidates = trace.stages["hybrid_fused"].candidates
    assert [(candidate.id, candidate.rank) for candidate in candidates] == [("a", 1), ("a", 2)]
    assert candidates[0].duplicate_of is None
    assert candidates[1].duplicate_of == "a"
    assert candidates[1].removal_reason.code == "duplicate"
    expected_score = 1 / 61 + 1 / 62
    assert candidates[0].scores["fused"] == pytest.approx(expected_score)
    assert candidates[1].scores["fused"] == pytest.approx(expected_score)


def test_rrf_stops_constructing_trace_candidates_when_the_request_budget_is_exhausted(monkeypatch):
    created = 0

    def counted_candidate(**kwargs):
        nonlocal created
        created += 1
        return TraceCandidate(**kwargs)

    monkeypatch.setattr(rrf_module, "TraceCandidate", counted_candidate)
    trace = RetrievalTraceBuilder(
        "req-1",
        "q",
        diagnostics=RetrievalDiagnosticsContext(candidate_limit=3),
    )
    chunks = [Chunk(id=f"chunk-{index}", text="private") for index in range(10)]

    rrf_reranking([chunks], key_fn=lambda chunk: chunk.id, trace=trace)

    assert created == 3
    assert trace.stages["hybrid_fused"].candidate_count == 10
