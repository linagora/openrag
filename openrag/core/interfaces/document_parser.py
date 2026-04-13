from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class DocumentParser(ABC):
    """Abstract interface for document parsing/loading.

    Implementations: BaseLoader subclasses (components/indexer/loaders/)
    """

    @abstractmethod
    async def aload_document(
        self,
        file_path: str | Path,
        metadata: dict | None = None,
        save_markdown: bool = False,
    ) -> Any:
        """Asynchronously load and parse a document file.

        Args:
            file_path: Path to the file to parse.
            metadata: Optional metadata to attach to the parsed document.
            save_markdown: Whether to save intermediate markdown output.

        Returns:
            A parsed Document object.
        """
        ...
