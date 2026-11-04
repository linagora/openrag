"""Consume tabular batches through the indexing stages, one at a time."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime

from core.indexing.topic_tags import TopicTagSample
from core.models.document import ProcessedDocument
from core.observability.metric_specs import STAGE_VALUES
from core.observability.ray_metrics import observe_stage_duration, record_parse_completion, seed_parse_watchdog
from core.utils.logging import get_logger
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials
from services.workers.stages.store import INDEXING_TASK_ID_METADATA_KEY
from services.workers.stages.topic_tag import topic_tag_stage

logger = get_logger()


class _AsyncBatchReader:
    """Read a synchronous iterator without making cancellation wait on its thread.

    Python cannot stop a thread already inside ``next()``.  On timeout or
    cancellation, keep the iterator alive until that read returns, then close
    it from the event-loop thread.  This lets the indexing task fail promptly
    without closing parser resources while they are still in use.
    """

    def __init__(self, batches):
        self.batches = batches
        self.pending: asyncio.Task | None = None
        self.read_finished: threading.Event | None = None
        self.closed = False

    def _read_one(self):
        assert self.read_finished is not None
        try:
            return next(self.batches, None)
        finally:
            self.read_finished.set()

    async def next(self):
        # next(..., None) avoids propagating StopIteration through a Future.
        self.read_finished = threading.Event()
        self.pending = asyncio.create_task(asyncio.to_thread(self._read_one))
        try:
            return await asyncio.shield(self.pending)
        finally:
            if self.pending.done() and self.read_finished.is_set():
                self.pending = None

    def _close(self):
        if self.closed:
            return
        self.closed = True
        close = getattr(self.batches, "close", None)
        if close is not None:
            close()

    async def aclose(self):
        pending = self.pending
        read_finished = self.read_finished
        if pending is None or read_finished is None or read_finished.is_set():
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            self._close()
            return

        async def close_after_read():
            # The asyncio Task may be cancelled while its worker thread is
            # still executing.  Wait on the thread-owned signal rather than
            # the Task so generator.close() never races next().
            await asyncio.to_thread(read_finished.wait)
            self._close()

        asyncio.create_task(close_after_read())


def _safe_tracking_call(tracker, method: str, *args, **kwargs) -> None:
    if tracker is None:
        return
    try:
        getattr(tracker, method)(*args, **kwargs)
    except Exception:  # noqa: BLE001 - stage tracking cannot fail indexing
        logger.warning("Indexing stage progress update failed", operation=method)


async def run_tabular_batches(
    pipeline,
    row,
    parser,
    *,
    batch_context,
    stage_tracker=None,
    topic_tagger=None,
    max_topic_tags=7,
):
    document = row["document"]
    tabular_format = parser.tabular_format
    format_label = tabular_format.upper()
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
    topic_sample = TopicTagSample() if topic_tagger is not None else None
    if topic_sample is not None:
        row.pop("topic_tags", None)

    row.pop("_replace_old_chunk_ids", None)
    row.pop("_replace_old_chunk_collection", None)
    old_ids = await pipeline.existing_chunk_ids(row) if row.get("replace") else []
    row["stored_count"] = 0

    try:
        seed_parse_watchdog(tabular_format)
        reader = _AsyncBatchReader(parser.iter_batches(document))
        try:
            while True:
                child = None
                row["stage"] = "parsing"
                _safe_tracking_call(stage_tracker, "start_stage", "parse", STAGE_VALUES.index("parse"))
                try:
                    if remaining_parse_time is not None and remaining_parse_time <= 0:
                        raise TimeoutError(f"{format_label} parsing exceeded the file's parse time budget")
                    start = time.perf_counter()
                    try:
                        batch = await run_with_optional_timeout(reader.next, remaining_parse_time)
                    finally:
                        elapsed = time.perf_counter() - start
                        observe_stage_duration("parse", elapsed)
                        if remaining_parse_time is not None:
                            remaining_parse_time -= elapsed
                except asyncio.CancelledError:
                    _safe_tracking_call(stage_tracker, "finish_stage", cancelled=True)
                    raise
                except Exception as exc:
                    _safe_tracking_call(stage_tracker, "finish_stage", error=exc)
                    raise
                else:
                    _safe_tracking_call(stage_tracker, "finish_stage")

                if batch is None:
                    record_parse_completion(tabular_format)
                    break

                batch_number += 1
                processed = ProcessedDocument(
                    document_id=document.id,
                    text_blocks=[batch],
                    metadata={
                        **document.metadata,
                        **batch.metadata,
                        "source": document.filename or document.metadata.get("source", ""),
                        "tabular_batch_index": batch_number,
                        f"{tabular_format}_batch_index": batch_number,
                    },
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
                await pipeline.run_processed_batch(
                    child,
                    processed,
                    stage_tracker=stage_tracker,
                    chunk_offset=chunk_offset,
                    indexed_at=indexed_at,
                    context=batch_context,
                )
                if topic_sample is not None:
                    topic_sample.add(child.get("chunks") or [])
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
                # requesting another table batch.
                child.clear()
                child = None
                del processed, batch
        finally:
            await reader.aclose()

        if topic_sample is not None:
            # The stage expects chunks, but only bounded text copies survive
            # here. Never retain the batches or their embedding vectors.
            row["chunks"] = topic_sample.chunks
            row["stage"] = "topic_tagging"
            _safe_tracking_call(stage_tracker, "start_stage", "topic_tag", STAGE_VALUES.index("topic_tag"))
            start = time.perf_counter()
            try:
                await topic_tag_stage(
                    row,
                    topic_tagger,
                    max_tags=max_topic_tags,
                    timeout=pipeline.timeouts.topic_tag,
                )
            except asyncio.CancelledError:
                _safe_tracking_call(stage_tracker, "finish_stage", cancelled=True)
                raise
            except Exception as exc:
                _safe_tracking_call(stage_tracker, "finish_stage", error=exc, degraded=True)
                # Match the existing optional-enrichment behavior. Cancellation
                # still propagates and triggers this attempt's vector cleanup.
                row["topic_tags"] = []
                row.setdefault("degraded_stages", {})["topic_tag"] = str(exc)
                logger.warning(f"{format_label} topic tagging failed; indexing the file without tags: {exc}")
            else:
                degraded_error = (row.get("degraded_stages") or {}).get("topic_tag")
                if degraded_error is None:
                    _safe_tracking_call(stage_tracker, "finish_stage")
                else:
                    _safe_tracking_call(
                        stage_tracker,
                        "finish_stage",
                        error=RuntimeError(str(degraded_error)),
                        degraded=True,
                    )
            finally:
                row.pop("chunks", None)
                observe_stage_duration("topic_tag", time.perf_counter() - start)

        # Replacement cleanup happens once, after every new batch succeeds.
        # Workers defer deletion until the catalog write also succeeds.
        if old_ids and stored_total:
            await pipeline.finalize_replacement(row, old_ids)
        row["stage"] = "stored"
        row["chunk_count"] = chunk_offset
        row["batch_count"] = batch_number
        row.pop("error", None)
        return row
    except BaseException as exc:
        if child:
            row["stage"] = child.get("stage", f"{tabular_format}_failed")
        else:
            row["stage"] = "topic_tag_failed" if row.get("stage") == "topic_tagging" else "parse_failed"
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
                logger.warning(f"{format_label} failure cleanup could not finish: {cleanup_error}")
        raise
    finally:
        row.pop("chunks", None)
        row.pop("processed_document", None)
        scrub_credentials(row)
