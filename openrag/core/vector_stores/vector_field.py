"""Names of the per-embedder dense vector fields.

Every embedder writes its vectors to a field of its own (``vector_bge_m3``) and
a partition searches only its embedder's field, so two embedders' vectors never
share an index. The name is allocated once, when the embedder is created, and
stored on its row: renaming the endpoint must not move its vectors.
"""

from __future__ import annotations

import re
from collections.abc import Collection

# Letters, digits and underscores: legal field names in every store we target.
_DISALLOWED = re.compile(r"[^0-9A-Za-z_]+")
_UNDERSCORE_RUNS = re.compile(r"_{2,}")

VECTOR_FIELD_PREFIX = "vector_"

LEGACY_VECTOR_FIELD = "vector"
"""The dense field every embedder shared before schema version 3."""

_MAX_FIELD_NAME_LENGTH = 255


def resolve_vector_field(vector_field: str | None) -> str:
    """The dense field an embedder reads and writes.

    Raises rather than falling back: an embedder without a field is one whose
    migrations have not run, and any other field holds other vectors.
    """
    if not vector_field:
        raise ValueError(
            "This embedder has no dense vector field. Apply the pending migrations "
            "(start OpenRAG once for the SQL ones, then the Milvus migration runner)."
        )
    return vector_field


def is_vector_field_key(key: object) -> bool:
    """Whether a row key is a dense vector field rather than chunk metadata."""
    return isinstance(key, str) and (key == LEGACY_VECTOR_FIELD or key.startswith(VECTOR_FIELD_PREFIX))


def allocate_vector_field_name(endpoint_name: str, taken: Collection[str]) -> str:
    """A readable field name for ``endpoint_name`` that is not in ``taken``.

    ``Qwen3-Embedding-0.6B`` becomes ``vector_Qwen3_Embedding_0_6B``; a name
    already taken gets a ``_2``, ``_3``, … suffix.
    """
    stem = _UNDERSCORE_RUNS.sub("_", _DISALLOWED.sub("_", endpoint_name)).strip("_") or "embedder"
    candidate = (VECTOR_FIELD_PREFIX + stem)[:_MAX_FIELD_NAME_LENGTH]
    attempt, n = candidate, 1
    while attempt in taken:
        n += 1
        suffix = f"_{n}"
        attempt = candidate[: _MAX_FIELD_NAME_LENGTH - len(suffix)] + suffix
    return attempt


__all__ = [
    "LEGACY_VECTOR_FIELD",
    "VECTOR_FIELD_PREFIX",
    "allocate_vector_field_name",
    "is_vector_field_key",
    "resolve_vector_field",
]
