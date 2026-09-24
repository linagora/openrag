"""What produced a file's vectors, as recorded in its catalog row.

Kept apart from :mod:`services.workers.pipeline_builder` so the dispatcher's
copy path, which records it from the API process, does not import the indexing
pipeline and every stage with it.
"""

from __future__ import annotations

from typing import Any

from core.embeddings.embedder import Embedder


def embedder_provenance(embedder: Embedder, reference: Any) -> dict[str, Any]:
    """What actually produced this file's vectors.

    ``embedder`` is the endpoint reference the partition carried, kept as given
    (the ``"default"`` alias included); the model/endpoint pair is what that
    reference resolved to, and is the only thing that catches an endpoint
    repointed at a different model without being renamed.

    Every field degrades to ``None`` rather than raising: describing a run that
    already succeeded must not be able to fail it.
    """
    try:
        dimension = embedder.dimension
    except Exception:
        # Raises until the first embed returns, so: no chunks, no dimension.
        dimension = None
    return {
        "embedder": str(reference) if reference else "default",
        "embedder_model_name": getattr(embedder, "model_name", None),
        "embedder_endpoint": getattr(embedder, "endpoint", None),
        "embedder_dimension": dimension,
    }
