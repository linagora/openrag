from __future__ import annotations

import asyncio
from collections.abc import MutableMapping, Sequence
from typing import Any

from services.workers.pipeline_builder import IndexingPipeline
from utils.logger import get_logger

_logger = get_logger().bind(component="batch_ingest")


async def ingest_batch(
    pipeline: IndexingPipeline,
    rows: Sequence[MutableMapping[str, Any]],
    *,
    concurrency: int | None = None,
) -> list[MutableMapping[str, Any]]:
    """Run each row through *pipeline*, capturing per-row exceptions.

    A failure on one row does not abort the others.  Each failed row will have
    ``row["stage"]`` set to the failing stage name and ``row["error"]`` set to
    the exception message — exactly as the individual stages do.

    Args:
        pipeline: The assembled indexing pipeline to run each row through.
        rows: Rows to process. Each row is a mutable mapping passed directly to
            ``pipeline.run()``.
        concurrency: Maximum number of rows processed concurrently.  ``None``
            means all rows are dispatched at once.  Must be >= 1 when provided.
    """
    if concurrency is not None and concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    sem = asyncio.Semaphore(concurrency) if concurrency is not None else None

    async def _run_one(row: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        if sem is not None:
            async with sem:
                await _pipeline_run_catching(pipeline, row)
        else:
            await _pipeline_run_catching(pipeline, row)
        return row

    return list(await asyncio.gather(*(_run_one(row) for row in rows)))


async def _pipeline_run_catching(
    pipeline: IndexingPipeline,
    row: MutableMapping[str, Any],
) -> None:
    try:
        await pipeline.run(row)
    except Exception as exc:
        row.setdefault("stage", "pipeline_failed")
        row.setdefault("error", str(exc))
        _logger.bind(stage=row.get("stage"), partition=row.get("partition")).warning(
            "Unhandled exception escaping pipeline stage",
            error=str(exc),
        )


__all__ = ["ingest_batch"]
