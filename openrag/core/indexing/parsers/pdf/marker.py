"""Marker-backed PDF ``DocumentParser`` (thin core facade).

Holds a reference to a ``BasePooledParser`` (the actual Ray-pool /
GPU-model / process-pool implementation lives in ``services/`` and is
not yet wired up) and delegates ``parse()`` to it. The split keeps core
free of Ray and GPU lifecycle code while still naming the Marker
backend as a first-class parser type.

The injected pool is a generic ``BasePooledParser``; if a more specific
``MarkerPoolParser`` ABC emerges in services, this class can tighten
its type without changing call sites.
"""

from __future__ import annotations

from ....models.document import Document, ProcessedDocument
from ..document_parser import BasePooledParser, DocumentParser
from ..registry import parser_registry


@parser_registry.register("marker")
class MarkerParser(DocumentParser):
    """Public PDF parser facade backed by a Marker worker pool."""

    def __init__(self, pool: BasePooledParser) -> None:
        # check pool is a BasePooledParser? and not empty
        if not isinstance(pool, BasePooledParser) or pool is None:
            raise ValueError("MarkerParser requires a BasePooledParser instance as pool")

        self._pool = pool

    def supported_types(self) -> list[str]:
        return self._pool.supported_types()

    async def parse(self, document: Document) -> ProcessedDocument:
        return await self._pool.parse(document)
