from abc import ABC, abstractmethod


class UserRepo(ABC):
    """Port for user management.

    Current implementation: PartitionFileManager (components/indexer/vectordb/utils.py)
    """

    @abstractmethod
    def create_user(
        self,
        display_name: str | None = None,
        external_user_id: str | None = None,
        is_admin: bool = False,
        file_quota: int | None = None,
    ) -> dict:
        """Create a new user. Returns dict with user info and plaintext token."""
        ...

    @abstractmethod
    def get_user_by_token(self, token: str) -> dict | None:
        """Look up a user by their API token. Returns None if not found."""
        ...

    @abstractmethod
    def get_user_by_id(self, user_id: int) -> dict | None:
        """Look up a user by their ID. Returns None if not found."""
        ...

    @abstractmethod
    def delete_user(self, user_id: int) -> bool:
        """Delete a user by ID."""
        ...

    @abstractmethod
    def list_users(self) -> list[dict]:
        """List all users."""
        ...

    @abstractmethod
    def regenerate_token(self, user_id: int) -> dict:
        """Regenerate a user's API token. Returns dict with new plaintext token."""
        ...

    @abstractmethod
    def update_quota(self, user_id: int, file_quota: int | None) -> dict:
        """Update a user's file quota."""
        ...

    @abstractmethod
    def list_user_partitions(self, user_id: int) -> list[dict]:
        """List all partitions a user has access to, with roles."""
        ...
