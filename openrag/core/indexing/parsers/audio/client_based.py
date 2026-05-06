"""Client-backed audio ``DocumentParser`` (thin core facade).

Holds a ``BaseClientParser`` (the actual HTTP-client / OpenAI-SDK
implementation lives in ``services/`` and is composed in at startup)
and delegates ``parse()`` to it.

Mirrors the :class:`ClientPdfParser` pattern: core stays free of vendor
SDKs while the facade names "client-backed audio" as a first-class
parser type.
"""

from __future__ import annotations

from ....models.document import Document, DocumentType, ProcessedDocument
from ..document_parser import BaseClientParser, DocumentParser
from ..registry import parser_registry


@parser_registry.register("audio_client")
class ClientAudioParser(DocumentParser):
    """Public audio parser facade backed by an OpenAI-compatible transcription client."""

    def __init__(self, client: BaseClientParser) -> None:
        if not isinstance(client, BaseClientParser):
            raise ValueError("ClientAudioParser requires a BaseClientParser instance as client")
        self._client = client

    def supported_types(self) -> list[str]:
        return [DocumentType.AUDIO.value, DocumentType.VIDEO.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await self._client.parse(document)
