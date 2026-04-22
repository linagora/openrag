"""Abstract document parser interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from openrag.core.models.document import Document, ProcessedDocument


class DocumentParser(ABC):
    """Base class for all document parsers (PDF, text, HTML, image, audio, etc.)."""

    @abstractmethod
    async def parse(self, document: Document) -> ProcessedDocument:
        """Parse a document into text blocks and images."""
        ...

    @abstractmethod
    def supported_types(self) -> list[str]:
        """Return list of DocumentType values this parser handles."""
        ...
