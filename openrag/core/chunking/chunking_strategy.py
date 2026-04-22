"""Abstract chunking strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from openrag.core.models.chunk import Chunk
from openrag.core.models.document import ProcessedDocument


class ChunkingStrategy(ABC):
    """Base class for all chunking strategies."""

    @abstractmethod
    def chunk(self, document: ProcessedDocument, partition: str = "default") -> list[Chunk]:
        """Split a processed document into chunks."""
        ...
