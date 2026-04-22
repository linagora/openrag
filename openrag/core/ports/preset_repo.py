"""Preset repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class PresetRepository(ABC):
    """CRUD operations for pipeline presets."""

    @abstractmethod
    async def get(self, name: str, preset_type: str) -> dict | None: ...

    @abstractmethod
    async def list_all(self, preset_type: str | None = None) -> list[dict]: ...

    @abstractmethod
    async def upsert(self, name: str, preset_type: str, config: dict) -> dict: ...

    @abstractmethod
    async def delete(self, name: str, preset_type: str) -> bool: ...
