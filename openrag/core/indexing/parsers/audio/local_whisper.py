"""Local Whisper-backed audio ``DocumentParser`` (thin core facade).

Holds a reference to a ``BasePooledParser`` (the actual Ray-pool /
GPU-model implementation lives in ``services/`` and is not yet wired
up) and delegates ``parse()`` to it. The split keeps core free of Ray
and GPU lifecycle code while still naming the local-Whisper backend as
a first-class parser type.

The injected pool is a generic ``BasePooledParser``; if a more specific
``WhisperPoolParser`` ABC emerges in services, this class can tighten
its type without changing call sites.
"""

from __future__ import annotations

from ....models.document import Document, DocumentType, ProcessedDocument
from ..document_parser import BasePooledParser, DocumentParser


class LocalWhisperParser(DocumentParser):
    """Public audio parser facade backed by a local-Whisper worker pool."""

    def __init__(self, pool: BasePooledParser) -> None:
        if not isinstance(pool, BasePooledParser):
            raise ValueError("LocalWhisperParser requires a BasePooledParser instance as pool")
        self._pool = pool

    def supported_types(self) -> list[str]:
        return [DocumentType.AUDIO.value, DocumentType.VIDEO.value]

    async def parse(self, document: Document) -> ProcessedDocument:
        return await self._pool.parse(document)
