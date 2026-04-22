"""Audit log repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AuditLogRepository(ABC):
    """Append-only audit trail."""

    @abstractmethod
    async def insert(
        self,
        user_id: int | None,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        details_json: dict | None = None,
        request_id: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def query(self, filters: dict[str, Any], offset: int = 0, limit: int = 50) -> list[dict]: ...
