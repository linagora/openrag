from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from core.indexing.parsers.document_parser import DocumentParser
from core.models.document import Document, ProcessedDocument
from services.workers.stages._common import run_with_optional_timeout, scrub_credentials


async def parse_stage(
    row: MutableMapping[str, Any],
    parser: DocumentParser,
    *,
    timeout: float | None = None,
) -> MutableMapping[str, Any]:
    """Parse ``row["document"]`` and mutate the row with the stage result."""

    try:
        document = row.get("document")
        if not isinstance(document, Document):
            raise ValueError("parse_stage row must contain a Document under 'document'")

        processed = await _parse_with_timeout(parser, document, timeout)
        row["processed_document"] = processed
        row["stage"] = "parsed"
        row.pop("error", None)
        return row
    except Exception as exc:
        row["stage"] = "parse_failed"
        row["error"] = str(exc)
        raise
    finally:
        scrub_credentials(row)


async def _parse_with_timeout(
    parser: DocumentParser,
    document: Document,
    timeout: float | None,
) -> ProcessedDocument:
    return await run_with_optional_timeout(lambda: parser.parse(document), timeout)
