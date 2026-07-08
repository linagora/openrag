"""Docling-backed PDF ``DocumentParser`` (thin core facade).

Holds a reference to a ``BasePooledParser`` (the Ray pool implementation
lives in ``services/workers/parsers/docling_workers.py``) and delegates
``parse()`` to it.  Keeping this facade in ``core/`` lets the indexing
pipeline reference the Docling backend without importing Ray.
"""

from __future__ import annotations

from ....models.document import Document, DocumentType, ProcessedDocument
from ..document_parser import BasePooledParser, DocumentParser
from ..registry import parser_registry


@parser_registry.register("docling")
class DoclingParser(DocumentParser):
    """Public PDF parser facade backed by a Docling worker pool."""

    def __init__(self, pool: BasePooledParser) -> None:
        if not isinstance(pool, BasePooledParser):
            raise ValueError("DoclingParser requires a BasePooledParser instance as pool")
        self._pool = pool

    def supported_types(self) -> list[str]:
        return [DocumentType.PDF.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await self._pool.parse(document)
