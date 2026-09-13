"""Naming rules for per-embedder dense vector fields (#762 F)."""

import re

import pytest
from core.vector_stores.vector_field import (
    LEGACY_VECTOR_FIELD,
    MAX_FIELD_NAME_LENGTH,
    allocate_vector_field_name,
    resolve_vector_field,
    sanitize_vector_field_name,
)

# The portable identifier subset the allocator targets — letters, digits and
# underscores, never leading with a digit — so a name stays legal whichever
# backend the store is running.
LEGAL_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class TestResolveVectorField:
    def test_none_means_the_legacy_shared_field(self):
        # Not "unset": rows predating the feature genuinely read and write
        # ``vector``, so None must resolve rather than raise or stay falsy.
        assert resolve_vector_field(None) == LEGACY_VECTOR_FIELD

    def test_an_allocated_name_is_returned_unchanged(self):
        assert resolve_vector_field("vector_bge_m3") == "vector_bge_m3"


class TestSanitize:
    @pytest.mark.parametrize(
        ("endpoint_name", "expected"),
        [
            ("bge_m3", "vector_bge_m3"),
            ("Qwen3-Embedding-0.6B", "vector_Qwen3_Embedding_0_6B"),
            ("jina.v3", "vector_jina_v3"),
            ("UPPER_lower_123", "vector_UPPER_lower_123"),
        ],
    )
    def test_readable_names_survive_the_charset(self, endpoint_name, expected):
        assert sanitize_vector_field_name(endpoint_name) == expected

    @pytest.mark.parametrize(
        "endpoint_name",
        ["bge_m3", "Qwen3-Embedding-0.6B", "jina.v3", "a--b", "...", "é", "a" * 400],
    )
    def test_every_result_is_a_legal_field_name(self, endpoint_name):
        name = sanitize_vector_field_name(endpoint_name)
        assert LEGAL_FIELD_NAME.fullmatch(name), name
        assert len(name) <= MAX_FIELD_NAME_LENGTH

    def test_underscore_runs_collapse(self):
        # "a--b" would otherwise become vector_a__b — legal, but noise.
        assert sanitize_vector_field_name("a--b") == "vector_a_b"

    def test_a_name_of_pure_punctuation_still_yields_a_usable_field(self):
        # A bare "vector_" prefix is not a legal field name; failing here
        # rather than at insert time keeps the cause next to the effect.
        assert sanitize_vector_field_name("...") == "vector_embedder"

    def test_overlong_names_are_truncated_to_the_ceiling(self):
        assert len(sanitize_vector_field_name("a" * 400)) == MAX_FIELD_NAME_LENGTH


class TestAllocate:
    def test_the_preferred_name_wins_when_free(self):
        assert allocate_vector_field_name("bge_m3", set()) == "vector_bge_m3"

    def test_a_collision_is_discriminated_not_overwritten(self):
        # "a-b" and "a.b" both prefer vector_a_b; the second must not be handed
        # the first one's field, which would merge two embedders' vectors.
        first = allocate_vector_field_name("a-b", set())
        second = allocate_vector_field_name("a.b", {first})
        assert first == "vector_a_b"
        assert second == "vector_a_b_2"
        assert first != second

    def test_repeated_collisions_keep_climbing(self):
        taken = {"vector_a_b", "vector_a_b_2", "vector_a_b_3"}
        assert allocate_vector_field_name("a.b", taken) == "vector_a_b_4"

    def test_the_legacy_field_can_never_be_claimed(self):
        # An endpoint literally named "vector" must not be handed the shared
        # legacy field, which would alias it onto every pre-existing row.
        assert allocate_vector_field_name("vector", set()) != LEGACY_VECTOR_FIELD

    def test_discriminated_names_stay_within_the_ceiling(self):
        preferred = sanitize_vector_field_name("a" * 400)
        allocated = allocate_vector_field_name("a" * 400, {preferred})
        assert len(allocated) <= MAX_FIELD_NAME_LENGTH
        assert allocated != preferred
        assert LEGAL_FIELD_NAME.fullmatch(allocated)
