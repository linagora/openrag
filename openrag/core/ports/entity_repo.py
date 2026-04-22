"""Entity repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class EntityRepository(ABC):
    """CRUD operations for extracted entities."""

    @abstractmethod
    async def upsert(self, partition: str, entity_type: str, canonical_name: str, aliases: list[str]) -> str: ...

    @abstractmethod
    async def search(self, partition: str, query: str, top_k: int = 10) -> list[dict]: ...

    @abstractmethod
    async def get_by_document(self, document_id: str) -> list[dict]: ...

    @abstractmethod
    async def delete_by_document(self, document_id: str) -> int: ...
