"""Consume CSV batches through the existing indexing stages, one at a time."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime

from core.models.document import ProcessedDocument
from core.observability.ray_metrics import observe_stage_duration, record_parse_completion, seed_parse_watchdog
from core.utils.logging import get_logger
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials
from services.workers.stages.store import INDEXING_TASK_ID_METADATA_KEY

logger = get_logger()


async def _next_batch(batches):
    # next(..., None) avoids propagating StopIteration through an asyncio Future.
    task = asyncio.create_task(asyncio.to_thread(next, batches, None))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A Python thread cannot be killed by cancelling the await. Wait for
        # this bounded read to finish before the surrounding context closes it.
        await asyncio.gather(task, return_exceptions=True)
        raise


async def run_csv_batches(pipeline, row, parser):
    document = row["document"]
    # A task-specific marker lets failure cleanup delete only this attempt.
    if not row.get("task_id"):
        row["task_id"] = str(uuid.uuid4())
    partition = str(row.get("partition") or document.partition)
    row["partition"] = partition
    indexed_at = datetime.now(UTC)
    stored_total = 0
    chunk_offset = 0
    batch_number = 0
    remaining_parse_time = pipeline.timeouts.parse
    store_attempted = False
    child = None

    row.pop("_replace_old_chunk_ids", None)
    row.pop("_replace_old_chunk_collection", None)
    old_ids = await pipeline._existing_chunk_ids(row) if row.get("replace") else []
    row["stored_count"] = 0

    try:
        seed_parse_watchdog("csv")
        with closing(parser.iter_batches(document)) as batches:
            while True:
                child = None
                row["stage"] = "parsing"
                if remaining_parse_time is not None and remaining_parse_time <= 0:
                    raise TimeoutError("CSV parsing exceeded the file's parse time budget")
                start = time.perf_counter()
                try:
                    batch = await run_with_optional_timeout(lambda: _next_batch(batches), remaining_parse_time)
                finally:
                    elapsed = time.perf_counter() - start
                    observe_stage_duration("parse", elapsed)
                    if remaining_parse_time is not None:
                        remaining_parse_time -= elapsed

                if batch is None:
                    record_parse_completion("csv")
                    break

                batch_number += 1
                processed = ProcessedDocument(
                    document_id=document.id,
                    text_blocks=[batch],
                    metadata={**document.metadata, "csv_batch_index": batch_number},
                )
                child = {
                    **row,
                    "replace": False,
                    "stored_count": 0,
                    "degraded_stages": dict(row.get("degraded_stages") or {}),
                }
                # Reuse the real chunking/embedding/storage path. Only parsing
                # is bypassed because this batch has already been parsed.
                store_attempted = True
                await pipeline.run(
                    child,
                    _processed=processed,
                    _chunk_offset=chunk_offset,
                    _indexed_at=indexed_at,
                )
                chunk_offset += len(child.get("chunks") or [])
                stored_total += child["stored_count"]
                row["stored_count"] = stored_total
                for key in (
                    "indexed_at",
                    "embedder_provenance",
                    "embedder_fingerprint",
                    "degraded_stages",
                ):
                    if key in child:
                        row[key] = child[key]

                # Release both the parsed table and its embedded chunks before
                # requesting another CSV batch.
                child.clear()
                child = None
                del processed, batch

        # Replacement cleanup happens once, after every new batch succeeds.
        # Workers defer deletion until the catalog write also succeeds.
        if old_ids and stored_total:
            if pipeline.defer_replace_cleanup:
                row["_replace_old_chunk_collection"] = "default"
                row["_replace_old_chunk_ids"] = old_ids
            else:
                await pipeline._delete_replaced_chunks(row, old_ids)
        row["stage"] = "stored"
        row["chunk_count"] = chunk_offset
        row["batch_count"] = batch_number
        row.pop("error", None)
        return row
    except BaseException as exc:
        row["stage"] = child.get("stage", "csv_failed") if child else "parse_failed"
        row["error"] = str(exc)
        # Milvus has no cross-batch transaction: cleanup is best-effort and
        # cannot make partial writes invisible while indexing is in progress.
        if store_attempted:
            try:
                await run_with_optional_timeout(
                    lambda: pipeline.vector_store.delete_by_filter(
                        {
                            "partition": partition,
                            "file_id": document.id,
                            INDEXING_TASK_ID_METADATA_KEY: str(row["task_id"]),
                        }
                    ),
                    pipeline.timeouts.store or 30.0,
                )
            except Exception as cleanup_error:
                logger.warning(f"CSV failure cleanup could not finish: {cleanup_error}")
        raise
    finally:
        row.pop("chunks", None)
        row.pop("processed_document", None)
        scrub_credentials(row)
