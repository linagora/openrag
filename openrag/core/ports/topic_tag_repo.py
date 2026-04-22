"""Topic tag repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class TopicTagRepository(ABC):
    """CRUD operations for document topic tags."""

    @abstractmethod
    async def bulk_insert(self, tags: list[dict]) -> int: ...

    @abstractmethod
    async def get_by_document(self, document_id: str) -> list[dict]: ...

    @abstractmethod
    async def delete_by_document(self, document_id: str) -> int: ...

    @abstractmethod
    async def search(self, partition: str, tag: str, top_k: int = 10) -> list[dict]: ...
