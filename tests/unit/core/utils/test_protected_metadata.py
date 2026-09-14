"""Caller-supplied metadata must not be able to overwrite server-managed keys."""

from core.utils.consts import strip_protected_metadata


def test_dense_vector_fields_are_protected():
    # The metadata-update path re-upserts whole rows with the caller's metadata
    # merged in, so a caller-set `vector_<embedder>` would replace that
    # embedder's vectors (#762 F).
    cleaned, removed = strip_protected_metadata(
        {"author": "alice", "vector": [0.1], "vector_bge_m3": [0.2], "source": "/etc/passwd"}
    )

    assert cleaned == {"author": "alice"}
    assert removed == ["source", "vector", "vector_bge_m3"]


def test_the_input_is_not_mutated():
    metadata = {"vector_bge_m3": [0.2]}

    strip_protected_metadata(metadata)

    assert metadata == {"vector_bge_m3": [0.2]}
