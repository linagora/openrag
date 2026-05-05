"""OpenAI-VLM-backed PDF ``DocumentParser`` (thin core facade).

Holds a ``BaseClientParser`` (the actual HTTP-client / OpenAI-SDK
implementation lives in ``services/`` and is composed in at startup) and
delegates ``parse()`` to it.

Mirrors the :class:`MarkerParser` pattern: core stays free of vendor
SDKs while the facade names "OpenAI-VLM PDF" as a first-class parser
type. Concrete subclasses of the services-side base (e.g. DotsOCR) can
be swapped in without changing this facade.
"""

from __future__ import annotations

from ....models.document import Document, ProcessedDocument
from ..document_parser import BaseClientParser, DocumentParser


class ClientPdfParser(DocumentParser):
    """Public PDF parser facade backed by an OpenAI-compatible VLM client."""

    def __init__(self, client: BaseClientParser) -> None:
        if not isinstance(client, BaseClientParser):
            raise ValueError("ClientPdfParser requires a BaseClientParser instance as client")
        self._client = client

    def supported_types(self) -> list[str]:
        return self._client.supported_types()

    async def parse(self, document: Document) -> ProcessedDocument:
        return await self._client.parse(document)
