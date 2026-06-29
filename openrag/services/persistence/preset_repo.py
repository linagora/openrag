"""asyncpg-backed :class:`PresetRepository`.

Manages the ``pipeline_presets`` table — named pipeline configuration
bundles for indexation and retrieval. Phase 14D replaces the earlier
stub with real SQL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from core.ports.preset_repo import PresetRepository
from core.utils.exceptions import NotFoundError

if TYPE_CHECKING:
    import asyncpg

_VALID_PRESET_COLUMNS = {
    "indexation": "indexation_preset",
    "retrieval": "retrieval_preset",
}

_UPSERT_PRESET_SQL = """
    INSERT INTO pipeline_presets (name, preset_type, config)
    VALUES ($1, $2, $3::jsonb)
    ON CONFLICT (name, preset_type) DO UPDATE
      SET config = EXCLUDED.config, updated_at = now()
    RETURNING *
    """


class PgPresetRepository(PresetRepository):
    """asyncpg-backed implementation of :class:`PresetRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    @staticmethod
    def _row_to_dict(row: asyncpg.Record) -> dict:
        return {
            "name": row["name"],
            "preset_type": row["preset_type"],
            "config": row["config"] or {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def get(self, name: str, preset_type: str) -> dict | None:
        rec = await self.pool.fetchrow(
            "SELECT * FROM pipeline_presets WHERE name = $1 AND preset_type = $2",
            name,
            preset_type,
        )
        return self._row_to_dict(rec) if rec else None

    async def list_all(self, preset_type: str | None = None) -> list[dict]:
        if preset_type is not None:
            rows = await self.pool.fetch(
                "SELECT * FROM pipeline_presets WHERE preset_type = $1 ORDER BY name",
                preset_type,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM pipeline_presets ORDER BY preset_type, name",
            )
        return [self._row_to_dict(r) for r in rows]

    async def upsert(self, name: str, preset_type: str, config: dict) -> dict:
        rec = await self.pool.fetchrow(
            _UPSERT_PRESET_SQL,
            name,
            preset_type,
            config,
        )
        return self._row_to_dict(rec)

    async def rename(self, old_name: str, new_name: str, preset_type: str, config: dict) -> dict:
        """Rename a preset to ``new_name`` (also updating its config) and repoint
        every referencing partition, atomically.

        Raises :class:`NotFoundError` if ``old_name`` no longer exists; a collision
        with an existing ``new_name`` is rejected by the ``(name, preset_type)``
        unique constraint. Returns the updated preset row as a dict.
        """
        # Rename in place with a plain UPDATE (not an upsert): the
        # (name, preset_type) unique constraint then rejects a collision with an
        # existing target name instead of an upsert silently overwriting it, and
        # the row keeps its identity/created_at. Partitions reference a preset by
        # its name string (partitions.{col}) with no FK, so referencing partitions
        # are repointed in the same transaction or they would orphan.
        col = _preset_column(preset_type)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rec = await conn.fetchrow(
                    "UPDATE pipeline_presets SET name = $2, config = $3::jsonb, updated_at = now() "
                    "WHERE name = $1 AND preset_type = $4 RETURNING *",
                    old_name,
                    new_name,
                    config,
                    preset_type,
                )
                if rec is None:
                    # old_name vanished between the service's existence check and
                    # this UPDATE (concurrent delete) — fail cleanly instead of
                    # blowing up in _row_to_dict(None).
                    raise NotFoundError(f"Preset '{old_name}' of type '{preset_type}' not found.")
                await conn.execute(
                    f"UPDATE partitions SET {col} = $1 WHERE {col} = $2",
                    new_name,
                    old_name,
                )
        return self._row_to_dict(rec)

    async def delete(self, name: str, preset_type: str) -> bool:
        result = await self.pool.execute(
            "DELETE FROM pipeline_presets WHERE name = $1 AND preset_type = $2",
            name,
            preset_type,
        )
        return result == "DELETE 1"

    async def count_partitions_using(self, name: str, preset_type: str) -> int:
        col = _preset_column(preset_type)
        return await self.pool.fetchval(
            f"SELECT COUNT(*)::int FROM partitions WHERE {col} = $1",
            name,
        )


def _preset_column(preset_type: str) -> str:
    try:
        return _VALID_PRESET_COLUMNS[preset_type]
    except KeyError as exc:
        raise ValueError(f"Invalid preset_type: {preset_type}") from exc


__all__ = ["PgPresetRepository"]
