"""Partition repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class PartitionRepository(ABC):
    """CRUD operations for partitions."""

    @abstractmethod
    async def create_partition(self, name: str, user_id: int | None = None) -> dict: ...

    @abstractmethod
    async def get_partition(self, name: str) -> dict | None: ...

    @abstractmethod
    async def list_partitions(self) -> list[dict]: ...

    @abstractmethod
    async def delete_partition(self, name: str) -> bool: ...

    @abstractmethod
    async def partition_exists(self, name: str) -> bool: ...
