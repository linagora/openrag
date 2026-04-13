from abc import ABC, abstractmethod
from typing import Any


class ChunkRepo(ABC):
    """Port for chunk storage operations.

    This is a new abstraction — chunk-level persistence is currently
    embedded in MilvusDB. This port separates the chunk CRUD contract
    from the vector search contract (VectorStore).
    """

    @abstractmethod
    async def store_chunks(self, chunks: list[Any], partition: str) -> None:
        """Store document chunks."""
        ...

    @abstractmethod
    async def get_chunks_by_file(
        self, file_id: str, partition: str, limit: int = 100
    ) -> list[Any]:
        """Get all chunks belonging to a specific file."""
        ...

    @abstractmethod
    async def get_chunk_by_id(self, chunk_id: str) -> Any | None:
        """Retrieve a single chunk by its ID. Returns None if not found."""
        ...

    @abstractmethod
    async def delete_chunks_by_file(
        self, file_id: str, partition: str
    ) -> None:
        """Delete all chunks belonging to a specific file."""
        ...
