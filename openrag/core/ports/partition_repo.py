from abc import ABC, abstractmethod


class PartitionRepo(ABC):
    """Port for partition management.

    Current implementation: PartitionFileManager (components/indexer/vectordb/utils.py)
    """

    @abstractmethod
    def create_partition(self, partition: str, user_id: int) -> None:
        """Create a new partition and assign the creator as owner."""
        ...

    @abstractmethod
    def delete_partition(self, partition: str) -> bool:
        """Delete a partition and all associated data."""
        ...

    @abstractmethod
    def list_partitions(self) -> list[dict]:
        """List all partitions."""
        ...

    @abstractmethod
    def partition_exists(self, partition: str) -> bool:
        """Check if a partition exists."""
        ...

    @abstractmethod
    def list_members(self, partition: str) -> list[dict]:
        """List all members of a partition with their roles."""
        ...

    @abstractmethod
    def add_member(self, partition: str, user_id: int, role: str) -> bool:
        """Add a user to a partition with a role (owner/editor/viewer)."""
        ...

    @abstractmethod
    def remove_member(self, partition: str, user_id: int) -> bool:
        """Remove a user from a partition."""
        ...

    @abstractmethod
    def update_member_role(
        self, partition: str, user_id: int, new_role: str
    ) -> bool:
        """Update a user's role within a partition."""
        ...
