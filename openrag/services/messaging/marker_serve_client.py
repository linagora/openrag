"""``MarkerServeClient`` — off-Ray Marker PDF parser client (``BaseClientParser``).

Submits the PDF to the marker-serve worker over the :class:`TaskQueue` and
rebuilds the ``ProcessedDocument`` from the result using the shared
``marker_format`` helpers, so the output is byte-for-byte the shape the Ray
``MarkerLoader`` produces. Idempotent by content hash (safe retries).

Dependencies are injected (the ``TaskQueue`` is composed in
``parser_dispatcher``), keeping this class pure and unit-testable with the
in-memory queue.

FILE HANDOFF — INTERIM: the PDF bytes are base64-inlined in the task payload.
This is bounded by the broker's max payload (fine for modest PDFs and the
single-L4 mode) and is the one non-production shortcut here. The production path
for large/scanned docs is object-store keys (E1); only the payload construction
below and the worker's decode change — this client's contract does not.
"""

from __future__ import annotations

import base64
import hashlib

from core.config import Settings
from core.indexing.parsers.document_parser import BaseClientParser
from core.models.document import Document, DocumentType, ProcessedDocument, TextBlock
from core.ports.task_queue import TaskQueue, TaskStatus
from core.utils.logging import get_logger
from services.workers.parsers.marker_format import build_image_blocks_from_encoded, split_pages

logger = get_logger()

MARKER_TOPIC = "marker.parse"


class MarkerServeClient(BaseClientParser):
    def __init__(self, config: Settings, queue: TaskQueue) -> None:
        self._config = config
        self._queue = queue
        self._timeout = config.loader.marker_timeout
        self._max_attempts = max(1, config.loader.marker_max_task_retry)

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        if not document.raw_bytes:
            return ProcessedDocument(document_id=document.id, metadata=dict(document.metadata))

        payload = {"file_bytes_b64": base64.b64encode(document.raw_bytes).decode()}
        idempotency_key = hashlib.sha256(document.raw_bytes).hexdigest()

        handle = await self._queue.submit(
            MARKER_TOPIC, payload, idempotency_key=idempotency_key, max_attempts=self._max_attempts
        )
        result = await handle.result(timeout=self._timeout)

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

    async def aclose(self) -> None:
        await self._queue.aclose()
