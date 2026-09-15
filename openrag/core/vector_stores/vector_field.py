"""Field names for per-embedder dense vectors (#762 F).

One collection holds every partition's chunks, so a single ``vector`` field
means every embedder's output shares one index — the condition that makes an
embedder swap silently corrupt retrieval. The fix is one dense field per
embedder (``vector_jina_v3``, ``vector_bge_m3``), which asks two things of the
backend: that a nullable vector field can be added and indexed on a live
collection, and that a search on that field skips rows where the field is null
rather than reading them as zero. Both are obligations on the adapter, stated
here because the scheme is unsafe without them.

This module owns the *names* only — allocating them and resolving which one an
endpoint reads and writes. Creating the fields in the store is a separate
concern.

Two rules make the scheme safe, and both are load-bearing:

**A name is allocated once and never recomputed.** Endpoint names are labels:
they can be renamed (#770 cascades a rename to ``partitions.embedder``), and
renaming must not move an endpoint's vectors to a different field. Nothing on
:class:`~core.vector_stores.vector_store.VectorStore` renames a field, so a
recomputed name would not follow the data — it would name a field that does
not exist. The allocated name is therefore stored on the row and travels with
the rename; ``vector_field`` is deliberately absent from the repository's
update allowlist. The corollary is a policy, enforced by the edit guard
(#762 C): editing an endpoint never changes which model it points at. An
operator who wants a different model creates a different endpoint.

**Every embedder owns a field; nothing shares ``vector``.** Collections before
schema version 3 kept every embedder's output in one ``vector`` field. The
upgrade allocates a name for each existing embedder (SQL revision
``c4e8f2a6b913``) and the Milvus v3 migration copies each partition's vectors
into its embedder's field before dropping ``vector``. So a missing field is
never "the legacy one": it is a deployment whose migrations have not run, and
:func:`resolve_vector_field` says so instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Collection

# Deliberately narrower than any single backend demands: letters, digits and
# underscores are the identifier charset every store we target accepts, so a
# name allocated here stays legal if the backend changes. Some stores also
# refuse a leading digit — the constant prefix below rules that out.
_DISALLOWED = re.compile(r"[^0-9A-Za-z_]+")
_UNDERSCORE_RUNS = re.compile(r"_{2,}")

VECTOR_FIELD_PREFIX = "vector_"
"""Marks a field as a per-embedder dense vector, and keeps names letter-led."""

LEGACY_VECTOR_FIELD = "vector"
"""The shared dense field of pre-v3 collections — reserved, never allocated."""

MAX_FIELD_NAME_LENGTH = 255
"""Field-name ceiling, set to the shortest limit among the stores we target."""

_FALLBACK_STEM = "embedder"
"""Used when a name sanitizes to nothing — e.g. one made entirely of dots."""


def resolve_vector_field(vector_field: str | None) -> str:
    """The dense field an endpoint reads and writes.

    Raises instead of falling back: an embedder without a field is one whose
    migrations have not run, and routing it anywhere would read or write the
    wrong vectors.
    """
    if not vector_field:
        raise ValueError(
            "This embedder has no dense vector field. Apply the pending migrations "
            "(start OpenRAG once for the SQL ones, then the Milvus migration runner)."
        )
    return vector_field


def is_vector_field_key(key: object) -> bool:
    """Whether a row key is a dense vector field rather than chunk metadata.

    Stores return every vector field with a wildcard read, so anything that
    shows a row's metadata has to drop these — one float array per embedder.
    """
    return isinstance(key, str) and (key == LEGACY_VECTOR_FIELD or key.startswith(VECTOR_FIELD_PREFIX))


def sanitize_vector_field_name(endpoint_name: str) -> str:
    """Build the preferred field name for an endpoint, ignoring collisions.

    Readability is the point — an operator reading a collection schema should
    be able to tell which endpoint a field belongs to — so the endpoint name is
    carried through as literally as the charset allows rather than hashed:
    ``Qwen3-Embedding-0.6B`` becomes ``vector_Qwen3_Embedding_0_6B``.

    The API already narrows endpoint names to ``[A-Za-z0-9._-]`` within 128
    characters, so in practice only ``.`` and ``-`` are rewritten and the
    length ceiling is never reached. Neither is assumed here: endpoints seeded
    from ``conf/config.yaml`` are created through the repository directly,
    without passing the request schema that enforces either rule.

    Collisions are possible by construction (``a-b`` and ``a.b`` both reduce to
    ``a_b``), and truncation at the length ceiling would add more, so this is
    only ever a *candidate*. :func:`allocate_vector_field_name` settles
    uniqueness.
    """
    stem = _DISALLOWED.sub("_", endpoint_name)
    stem = _UNDERSCORE_RUNS.sub("_", stem).strip("_")
    if not stem:
        # Every character was punctuation. A bare prefix is not a legal field
        # name, and silently returning one would fail far from the cause.
        stem = _FALLBACK_STEM
    return (VECTOR_FIELD_PREFIX + stem)[:MAX_FIELD_NAME_LENGTH]


def allocate_vector_field_name(endpoint_name: str, taken: Collection[str]) -> str:
    """A field name for ``endpoint_name`` that no other endpoint already holds.

    ``taken`` is every ``vector_field`` currently allocated. The preferred name
    wins when it is free; otherwise a ``_2``, ``_3``, … discriminator is
    appended, trimming the stem when needed so the result stays inside
    :data:`MAX_FIELD_NAME_LENGTH`.

    ``vector`` itself is always treated as taken: it is the shared field of
    pre-v3 collections, so no endpoint may claim it as its own.
    """
    reserved = {LEGACY_VECTOR_FIELD, *taken}
    candidate = sanitize_vector_field_name(endpoint_name)
    if candidate not in reserved:
        return candidate

    for suffix_n in range(2, len(reserved) + 3):
        suffix = f"_{suffix_n}"
        stem = candidate[: MAX_FIELD_NAME_LENGTH - len(suffix)]
        attempt = stem + suffix
        if attempt not in reserved:
            return attempt
    # Unreachable: the loop tries more distinct names than there are reserved
    # ones. Raise rather than return a duplicate the unique index would reject.
    raise RuntimeError(f"Could not allocate a vector field name for '{endpoint_name}'.")


__all__ = [
    "LEGACY_VECTOR_FIELD",
    "MAX_FIELD_NAME_LENGTH",
    "VECTOR_FIELD_PREFIX",
    "allocate_vector_field_name",
    "is_vector_field_key",
    "resolve_vector_field",
    "sanitize_vector_field_name",
]
