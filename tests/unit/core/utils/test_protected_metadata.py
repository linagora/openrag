"""Caller-supplied metadata must not be able to overwrite server-managed keys."""

from core.utils.consts import strip_protected_metadata


def test_dense_vector_fields_are_protected():
    # A metadata update re-upserts whole rows with the caller's metadata merged in.
    cleaned, removed = strip_protected_metadata(
        {"author": "alice", "vector": [0.1], "vector_bge_m3": [0.2], "source": "/etc/passwd"}
    )

    assert cleaned == {"author": "alice"}
    assert removed == ["source", "vector", "vector_bge_m3"]
