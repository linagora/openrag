from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from core.indexing.topic_tags import TopicTagger
from core.models.chunk import Chunk
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials


async def topic_tag_stage(
    row: MutableMapping[str, Any],
    topic_tagger: TopicTagger,
    *,
    max_tags: int = 7,
    timeout: float | None = None,
) -> MutableMapping[str, Any]:
    """Extract document-level topic tags into ``row["topic_tags"]``."""
    try:
        chunks = row.get("chunks")
        if not _is_chunk_list(chunks):
            raise ValueError("topic_tag_stage row must contain a list[Chunk] under 'chunks'")

        filename = str(row.get("filename") or "")
        language = str(row.get("language") or row.get("lang") or "en")
        row["topic_tags"] = await run_with_optional_timeout(
            lambda: topic_tagger.tag(chunks, filename=filename, max_tags=max_tags, lang=language),
            timeout,
        )
        row["stage"] = "topic_tagged"
        row.pop("error", None)
        return row
    except Exception as exc:
        row["stage"] = "topic_tag_failed"
        row["error"] = str(exc)
        raise
    finally:
        scrub_credentials(row)


def _is_chunk_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(chunk, Chunk) for chunk in value)
