"""Model endpoint repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.config.model_endpoints import ModelEndpointRow


class ModelEndpointRepository(ABC):
    """CRUD operations for named model endpoint configurations."""

    @abstractmethod
    async def create(self, row: ModelEndpointRow) -> ModelEndpointRow: ...

    @abstractmethod
    async def get(self, name: str, model_type: str) -> ModelEndpointRow | None: ...

    @abstractmethod
    async def list_all(self, model_type: str | None = None) -> list[ModelEndpointRow]: ...

    @abstractmethod
    async def update(self, name: str, model_type: str, **fields: object) -> ModelEndpointRow | None: ...

    @abstractmethod
    async def rename(self, name: str, model_type: str, new_name: str) -> None: ...

    @abstractmethod
    async def delete(self, name: str, model_type: str) -> bool: ...

    @abstractmethod
    async def set_default(self, model_type: str, name: str) -> None: ...

    @abstractmethod
    async def delete_and_promote_default(self, name: str, model_type: str) -> tuple[str, str | None]:
        """Atomically delete an endpoint and, if it was the default, promote a
        survivor. Decides under a row lock. Returns ``(status, promoted_name)``
        where status is ``"not_found" | "last" | "ok"``."""
        ...
