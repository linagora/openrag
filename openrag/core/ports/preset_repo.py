"""Preset repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class PresetRepository(ABC):
    """CRUD operations for pipeline presets."""

    @abstractmethod
    async def get(self, name: str, preset_type: str) -> dict | None: ...

    @abstractmethod
    async def list_all(self, preset_type: str | None = None) -> list[dict]: ...

    @abstractmethod
    async def latest_updated_at(self) -> datetime | None:
        """Return the revision timestamp of the newest preset, if any."""
        ...

    @abstractmethod
    async def upsert(self, name: str, preset_type: str, config: dict) -> dict: ...

    @abstractmethod
    async def rename(self, old_name: str, new_name: str, preset_type: str, config: dict) -> dict: ...

    @abstractmethod
    async def delete(self, name: str, preset_type: str) -> bool:
        """Delete a preset. Raises ConflictError if any partition still references it."""
        ...

    @abstractmethod
    async def count_partitions_using(self, name: str, preset_type: str) -> int: ...

    @abstractmethod
    async def usage_counts(self) -> dict[tuple[str, str], int]:
        """Return ``{(name, preset_type): partition_count}`` for every referenced preset."""
        ...
