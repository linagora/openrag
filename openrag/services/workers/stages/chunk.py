from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import Any

from core.chunking.chunking_strategy import ChunkingStrategy
from core.models.chunk import Chunk
from core.models.document import ProcessedDocument
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials


async def chunk_stage(
    row: MutableMapping[str, Any],
    chunker: ChunkingStrategy,
    *,
    timeout: float | None = None,
) -> MutableMapping[str, Any]:
    """Chunk ``row["processed_document"]`` and mutate the row with chunks."""

    try:
        processed_document = row.get("processed_document")
        if not isinstance(processed_document, ProcessedDocument):
            raise ValueError("chunk_stage row must contain a ProcessedDocument under 'processed_document'")

        partition = str(row.get("partition") or "default")
        chunks = await _chunk_with_timeout(chunker, processed_document, partition, timeout)
        row["chunks"] = chunks
        row["stage"] = "chunked"
        row.pop("error", None)
        return row
    except Exception as exc:
        row["stage"] = "chunk_failed"
        row["error"] = str(exc)
        raise
    finally:
        scrub_credentials(row)


async def _chunk_with_timeout(
    chunker: ChunkingStrategy,
    processed_document: ProcessedDocument,
    partition: str,
    timeout: float | None,
) -> list[Chunk]:
    """Run the synchronous chunker without blocking the event loop."""

    async def run() -> list[Chunk]:
        return await asyncio.to_thread(chunker.chunk, processed_document, partition)

    return await run_with_optional_timeout(run, timeout)
