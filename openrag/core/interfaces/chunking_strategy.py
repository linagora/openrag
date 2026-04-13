from abc import ABC, abstractmethod
from typing import Any


class ChunkingStrategy(ABC):
    """Abstract interface for document chunking strategies.

    Implementations: BaseChunker subclasses (components/indexer/chunker/chunker.py)
    """

    @abstractmethod
    async def split_document(
        self, doc: Any, task_id: str | None = None
    ) -> list[Any]:
        """Split a document into chunks.

        Args:
            doc: The document to split (implementation defines the type).
            task_id: Optional task ID for progress tracking.

        Returns:
            List of document chunks with metadata.
        """
        ...
