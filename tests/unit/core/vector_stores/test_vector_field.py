"""Naming rules for per-embedder dense vector fields."""

import pytest
from core.vector_stores.vector_field import allocate_vector_field_name, is_vector_field_key, resolve_vector_field


@pytest.mark.parametrize("missing", [None, ""])
def test_a_missing_field_is_refused_not_mapped_to_a_shared_one(missing):
    with pytest.raises(ValueError, match="migrations"):
        resolve_vector_field(missing)


@pytest.mark.parametrize(
    ("key", "is_field"),
    [("vector", True), ("vector_bge_m3", True), ("text", False), ("vectorize", False), (3, False)],
)
def test_vector_field_keys(key, is_field):
    assert is_vector_field_key(key) is is_field


@pytest.mark.parametrize(
    ("endpoint_name", "expected"),
    [
        ("Qwen3-Embedding-0.6B", "vector_Qwen3_Embedding_0_6B"),
        ("a--b", "vector_a_b"),
        ("...", "vector_embedder"),
    ],
)
def test_names_stay_readable_and_legal(endpoint_name, expected):
    assert allocate_vector_field_name(endpoint_name, set()) == expected


def test_a_taken_name_gets_the_next_free_suffix():
    taken = {"vector_a_b", "vector_a_b_2"}
    assert allocate_vector_field_name("a.b", taken) == "vector_a_b_3"


def test_a_suffixed_name_stays_within_the_length_limit():
    long_name = "a" * 400
    first = allocate_vector_field_name(long_name, set())
    second = allocate_vector_field_name(long_name, {first})
    assert len(first) == len(second) == 255
    assert second.endswith("_2")
