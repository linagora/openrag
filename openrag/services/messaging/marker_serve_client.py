"""``MarkerServeClient`` — off-Ray Marker PDF parser client (``BaseClientParser``).

Submits the PDF to the marker-serve worker over the :class:`TaskQueue` and
rebuilds the ``ProcessedDocument`` from the result using the shared
``marker_format`` helpers, so the output is byte-for-byte the shape the Ray
``MarkerLoader`` produces. Idempotent by content hash (safe retries).

Dependencies are injected (the ``TaskQueue`` and ``ObjectStore`` are composed in
``parser_dispatcher``), keeping this class pure and unit-testable with the
in-memory backends.

FILE HANDOFF (E1): the PDF bytes are uploaded to the object store under a
content-addressed key; only that key travels in the task payload, so large or
scanned PDFs never hit the broker's max-payload limit. The client owns the
object's lifecycle — it best-effort deletes the object once a *terminal* result
is in hand (never on timeout, when a retrying worker may still need it); a bucket
TTL policy reaps anything a crashed producer leaves behind.
"""

from __future__ import annotations

import hashlib

from core.config import Settings
from core.indexing.parsers.document_parser import BaseClientParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from core.ports.object_store import ObjectStore
from core.ports.task_queue import TaskQueue, TaskStatus
from core.utils.logging import get_logger
from services.workers.parsers.marker_format import build_image_blocks_from_encoded, split_pages

logger = get_logger()

MARKER_TOPIC = "marker.parse"
# Content-addressed scratch key: the queue's idempotency (keyed on the same
# hash) guarantees one task/reader/deleter per unique file, so this is race-free.
OBJECT_KEY_PREFIX = "handoff"


class MarkerServeClient(BaseClientParser):
    def __init__(self, config: Settings, queue: TaskQueue, object_store: ObjectStore) -> None:
        self._config = config
        self._queue = queue
        self._object_store = object_store
        self._timeout = config.loader.marker_timeout
        self._max_attempts = max(1, config.loader.marker_max_task_retry)

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(document_id=document.id, metadata=dict(document.metadata))

        content_hash = hashlib.sha256(document.raw_bytes).hexdigest()
        object_key = f"{OBJECT_KEY_PREFIX}/{content_hash}.pdf"

        await self._object_store.put(object_key, document.raw_bytes, content_type="application/pdf")
        handle = await self._queue.submit(
            MARKER_TOPIC,
            {"object_key": object_key},
            idempotency_key=content_hash,
            max_attempts=self._max_attempts,
        )

        # A timeout/cancel leaves the task possibly still running (and still
        # needing the object), so only clean up once a terminal result is in hand.
        result = await handle.result(timeout=self._timeout)
        await self._delete_quietly(object_key)

        if result.status is not TaskStatus.SUCCEEDED:
            raise RuntimeError(f"marker-serve parse failed for document {document.id}: {result.error}")

        data = result.result or {}
        pages = split_pages(data.get("markdown", ""))
        return ProcessedDocument(
            document_id=document.id,
            text_blocks=[TextBlock(text=text, page_number=page) for page, text in pages],
            images=build_image_blocks_from_encoded(data.get("images", {})),
            metadata=dict(document.metadata),
            page_count=pages[-1][0] if pages else 0,
        )

    async def _delete_quietly(self, object_key: str) -> None:
        try:
            await self._object_store.delete(object_key)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort; TTL is the backstop
            logger.warning(f"failed to delete handoff object {object_key!r}: {exc}")

    async def aclose(self) -> None:
        await self._queue.aclose()
        await self._object_store.aclose()
