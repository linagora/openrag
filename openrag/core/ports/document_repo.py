"""Document repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openrag.core.models.catalog import DocumentRecord


class DocumentRepository(ABC):
    """CRUD operations for documents."""

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
    async def get_file_ids_by_relationship(self, partition: str, relationship_id: str) -> list[str]: ...

    @abstractmethod
    async def get_ancestor_file_ids(
        self, partition: str, file_id: str, max_ancestor_depth: int | None = None
    ) -> list[str]: ...
