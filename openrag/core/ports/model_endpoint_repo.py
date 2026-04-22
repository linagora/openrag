"""Model endpoint repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ModelEndpointRepository(ABC):
    """CRUD operations for model endpoint configurations."""

    @abstractmethod
    async def get(self, name: str, model_type: str) -> dict | None: ...

    @abstractmethod
    async def list_all(self, model_type: str | None = None) -> list[dict]: ...

    @abstractmethod
    async def upsert(self, name: str, model_type: str, config: dict) -> dict: ...

    @abstractmethod
    async def delete(self, name: str, model_type: str) -> bool: ...
