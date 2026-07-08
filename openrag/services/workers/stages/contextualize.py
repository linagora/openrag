from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from core.indexing.contextualize import ChunkContextualizer
from core.models.chunk import Chunk
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials, stage_timeout


async def contextualize_stage(
    row: MutableMapping[str, Any],
    contextualizer: ChunkContextualizer,
    *,
    timeout: float | None = None,
    per_chunk_timeout: float = 0.0,
) -> MutableMapping[str, Any]:
    """Contextualize ``row["chunks"]`` in place."""

    try:
        chunks = row.get("chunks")
        if not _is_chunk_list(chunks):
            raise ValueError("contextualize_stage row must contain a list[Chunk] under 'chunks'")

        filename = str(row.get("filename") or "")
        language = str(row.get("language") or row.get("lang") or "en")
        effective_timeout = stage_timeout(timeout, len(chunks), per_item_timeout=per_chunk_timeout)
        row["chunks"] = await run_with_optional_timeout(
            lambda: contextualizer.contextualize(chunks, filename=filename, lang=language),
            effective_timeout,
        )
        row["stage"] = "contextualized"
        row.pop("error", None)
        return row
    except Exception as exc:
        row["stage"] = "contextualize_failed"
        row["error"] = str(exc)
        raise
    finally:
        scrub_credentials(row)


def _is_chunk_list(value: Any) -> bool:
    """Return whether ``value`` is a concrete list of domain chunks."""
    return isinstance(value, list) and all(isinstance(chunk, Chunk) for chunk in value)
