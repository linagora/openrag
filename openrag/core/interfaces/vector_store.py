from abc import ABC, abstractmethod
from typing import Any


class VectorStore(ABC):
    """Abstract interface for vector database operations.

    Implementations: MilvusDB (components/indexer/vectordb/vectordb.py)

    Method signatures mirror the existing BaseVectorDB abstract methods.
    """

    @abstractmethod
    async def list_collections(self) -> list[str]:
        """List all collections in the vector store."""
        ...

    @abstractmethod
    def collection_exists(self, collection_name: str) -> bool:
        """Check if a collection exists."""
        ...

    @abstractmethod
    def list_partitions(self) -> list[dict]:
        """List all partitions across collections."""
        ...

    @abstractmethod
    def partition_exists(self, partition: str) -> bool:
        """Check if a partition exists."""
        ...

    @abstractmethod
    async def delete_partition(self, partition: str) -> None:
        """Delete a partition and all its data."""
        ...

    @abstractmethod
    def list_partition_files(self, partition: str, limit: int | None = None) -> dict:
        """List files within a partition."""
        ...

    @abstractmethod
    async def delete_file(self, file_id: str, partition: str) -> None:
        """Delete a file and its chunks from the partition."""
        ...

    @abstractmethod
    async def add_documents(self, chunks: list[Any], user: dict) -> None:
        """Insert document chunks with embeddings into the store.

        Args:
            chunks: List of document chunks (implementation defines the type).
            user: User info dict for tracking who uploaded.
        """
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.60,
        partition: list[str] | None = None,
        filter: dict | None = None,
        with_surrounding_chunks: bool = False,
    ) -> list[Any]:
        """Perform a similarity search.

        Args:
            query: The search query text.
            top_k: Number of results to return.
            similarity_threshold: Minimum similarity score.
            partition: Partitions to search within.
            filter: Additional metadata filters.
            with_surrounding_chunks: Whether to include neighboring chunks.

        Returns:
            List of matching documents with scores.
        """
        ...

    @abstractmethod
    async def multi_query_search(
        self,
        partition: list[str],
        queries: list[str],
        top_k_per_query: int = 5,
        similarity_threshold: float = 0.6,
        filter: dict | None = None,
        with_surrounding_chunks: bool = False,
    ) -> list[Any]:
        """Perform similarity search with multiple query variations.

        Results are merged and deduplicated across all queries.
        """
        ...

    @abstractmethod
    async def list_all_chunks(
        self, partition: str, include_embedding: bool = True
    ) -> list[Any]:
        """List all chunks in a partition."""
        ...

    @abstractmethod
    async def get_file_chunks(
        self,
        file_id: str,
        partition: str,
        include_id: bool = False,
        limit: int = 100,
    ) -> list[Any]:
        """Get all chunks belonging to a specific file."""
        ...

    @abstractmethod
    async def get_chunk_by_id(self, chunk_id: str) -> Any:
        """Retrieve a single chunk by its ID."""
        ...
