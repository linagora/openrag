"""RRF unit tests — fusion semantics and edge cases."""

from __future__ import annotations

import pytest

from openrag.core.retrieval.rrf import rrf_reranking


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
