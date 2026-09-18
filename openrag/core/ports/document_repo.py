"""Document repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.models.catalog import DocumentRecord


@dataclass(frozen=True, slots=True)
class ContentClaimLease:
    """Versioned content reservation eligible for orphan recovery."""

    file_id: str
    partition: str
    content_sha256: str
    claim_token: str
    expires_at: datetime


class DocumentRepository(ABC):
    """CRUD operations for documents."""

    @abstractmethod
    async def get_indexed_documents(self, keys: Collection[tuple[str, str]]) -> dict[tuple[str, str], datetime]:
        """Return existing (partition, file_id) keys and their indexing times.

        Implementations must perform a fresh, batched lookup and propagate errors.
        """
        raise NotImplementedError

    @abstractmethod
    async def list_indexed_documents(
        self, partition: str, *, before: datetime, after: str | None = None, limit: int = 500
    ) -> list[str]:
        """Keyset page of file IDs indexed before a cutoff, ordered by file ID."""
        raise NotImplementedError

    @abstractmethod
    async def create_document(self, doc: DocumentRecord) -> DocumentRecord: ...

    @abstractmethod
    async def get_document(self, document_id: str) -> DocumentRecord | None: ...

    @abstractmethod
    async def list_documents(
        self,
        partition: str | list[str] | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[DocumentRecord]: ...

    @abstractmethod
    async def update_document(self, document_id: str, **fields: Any) -> DocumentRecord | None: ...

    @abstractmethod
    async def delete_document(self, document_id: str) -> bool: ...

    @abstractmethod
    async def delete_documents_by_partition(self, partition: str) -> int: ...

    @abstractmethod
    async def count_documents(self, partition: str | list[str] | None = None, status: str | None = None) -> int: ...

    @abstractmethod
    async def file_exists_in_partition(self, file_id: str, partition: str) -> bool: ...

    @abstractmethod
    async def mark_file_independently_indexed(self, file_id: str, partition: str) -> bool:
        """Protect a file from workspace-owned cleanup."""
        ...

    @abstractmethod
    async def finalize_file_workspace_ownership(self, file_id: str, partition: str, workspace_ids: list[str]) -> bool:
        """Transfer a new upload to its workspaces only if all attachments remain."""
        ...

    @abstractmethod
    async def get_content_sha256(self, file_id: str, partition: str) -> str | None: ...

    @abstractmethod
    async def claim_content_sha256(
        self,
        *,
        file_id: str,
        partition: str,
        content_sha256: str,
        claim_token: str,
        replace: bool = False,
    ) -> str | None:
        """Reserve content and return the conflicting file ID when occupied."""
        ...

    @abstractmethod
    async def get_recoverable_content_sha256_claim(
        self,
        *,
        partition: str,
        content_sha256: str,
    ) -> ContentClaimLease | None:
        """Return an aged indexing lease that may be orphaned."""
        ...

    @abstractmethod
    async def release_recoverable_content_sha256_claim(self, lease: ContentClaimLease) -> bool:
        """Release the lease only if its owner and version are unchanged."""
        ...

    @abstractmethod
    async def renew_content_sha256_claim(
        self,
        *,
        file_id: str,
        partition: str,
        content_sha256: str,
        claim_token: str,
    ) -> bool:
        """Extend an active content claim owned by this indexing attempt."""
        ...

    @abstractmethod
    async def release_content_sha256_claim(
        self,
        *,
        file_id: str,
        partition: str,
        content_sha256: str,
        claim_token: str,
    ) -> None: ...

    @abstractmethod
    async def get_file_ids_by_relationship(self, partition: str, relationship_id: str) -> list[str]: ...

    @abstractmethod
    async def get_ancestor_file_ids(
        self, partition: str, file_id: str, max_ancestor_depth: int | None = None
    ) -> list[str]: ...
