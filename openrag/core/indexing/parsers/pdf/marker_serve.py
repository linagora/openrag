"""Off-Ray Marker PDF ``DocumentParser`` (thin core facade).

Holds a ``BaseClientParser`` — the TaskQueue-backed marker-serve client whose
concrete impl lives in ``services/`` — and delegates ``parse()`` to it. Mirrors
:class:`MarkerParser`: core stays free of queue/Ray details while naming the
off-Ray Marker backend as a first-class parser type, selectable via
``config.loader.file_loaders.pdf = "MarkerServeLoader"``.
"""

from __future__ import annotations

from ....models.document import Document, ProcessedDocument
from ..document_parser import BaseClientParser, DocumentParser
from ..registry import parser_registry


@parser_registry.register("marker_serve")
class MarkerServeParser(DocumentParser):
    """Public PDF parser facade backed by the marker-serve worker (off Ray)."""

    def __init__(self, client: BaseClientParser) -> None:
        if not isinstance(client, BaseClientParser):
            raise ValueError("MarkerServeParser requires a BaseClientParser instance as client")
        self._client = client

    def supported_types(self) -> list[str]:
        return self._client.supported_types()

    async def parse(self, document: Document) -> ProcessedDocument:
        return await self._client.parse(document)
