"""Idempotency key repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class IdempotencyRepository(ABC):
    """Cache for request idempotency keys."""

    @abstractmethod
    async def get_by_hash(self, key_hash: str) -> dict | None: ...

    @abstractmethod
    async def store(self, key_hash: str, http_method: str, status_code: int, response_body: bytes) -> None: ...
