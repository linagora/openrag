from abc import ABC, abstractmethod


class DocumentRepo(ABC):
    """Port for file/document metadata persistence.

    Current implementation: PartitionFileManager (components/indexer/vectordb/utils.py)
    """

    @abstractmethod
    def add_file(
        self,
        file_id: str,
        partition: str,
        file_metadata: dict | None = None,
        user_id: int | None = None,
        relationship_id: str | None = None,
        parent_id: str | None = None,
    ) -> bool:
        """Add a file record to a partition."""
        ...

    @abstractmethod
    def remove_file(self, file_id: str, partition: str) -> bool:
        """Remove a file record from a partition."""
        ...

    @abstractmethod
    def list_files(self, partition: str, limit: int | None = None) -> dict:
        """List files in a partition with optional limit.

        Returns:
            Dict with 'total_count' and 'files' keys.
        """
        ...

    @abstractmethod
    def file_exists(self, file_id: str, partition: str) -> bool:
        """Check if a file exists in a partition."""
        ...

    @abstractmethod
    def get_file_count(self, partition: str) -> int:
        """Get the number of files in a partition."""
        ...

    @abstractmethod
    def get_total_file_count(self) -> int:
        """Get the total number of files across all partitions."""
        ...

    @abstractmethod
    def get_files_by_relationship(
        self, partition: str, relationship_id: str
    ) -> list[dict]:
        """Get all files sharing a relationship ID."""
        ...

    @abstractmethod
    def get_file_ids_by_relationship(
        self, partition: str, relationship_id: str
    ) -> list[str]:
        """Get file IDs sharing a relationship ID."""
        ...

    @abstractmethod
    def get_file_ancestors(
        self,
        partition: str,
        file_id: str,
        max_depth: int | None = None,
    ) -> list[dict]:
        """Get ancestor files following the parent_id chain."""
        ...

    @abstractmethod
    def get_ancestor_file_ids(
        self,
        partition: str,
        file_id: str,
        max_depth: int | None = None,
    ) -> list[str]:
        """Get ancestor file IDs following the parent_id chain."""
        ...
