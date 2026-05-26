from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from core.embeddings.embedder import Embedder
from core.models.chunk import Chunk
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials, stage_timeout


async def embed_stage(
    row: MutableMapping[str, Any],
    embedder: Embedder,
    *,
    timeout: float | None = None,
    per_chunk_timeout: float = 0.0,
) -> MutableMapping[str, Any]:
    """Embed ``row["chunks"]`` and replace them with embedded copies."""

    try:
        chunks = row.get("chunks")
        if not _is_chunk_list(chunks):
            raise ValueError("embed_stage row must contain a list[Chunk] under 'chunks'")

        effective_timeout = stage_timeout(timeout, len(chunks), per_item_timeout=per_chunk_timeout)
        texts = [chunk.text for chunk in chunks]
        vectors = await run_with_optional_timeout(lambda: embedder.embed(texts), effective_timeout)
        if len(vectors) != len(chunks):
            raise ValueError("embedder returned a different number of vectors than chunks")
        row["chunks"] = [chunk.with_embedding(vector) for chunk, vector in zip(chunks, vectors, strict=True)]
        row["stage"] = "embedded"
        row.pop("error", None)
        return row
    except Exception as exc:
        row["stage"] = "embed_failed"
        row["error"] = str(exc)
        raise
    finally:
        scrub_credentials(row)


def _is_chunk_list(value: Any) -> bool:
    """Return whether ``value`` is a concrete list of domain chunks."""
    return isinstance(value, list) and all(isinstance(chunk, Chunk) for chunk in value)
