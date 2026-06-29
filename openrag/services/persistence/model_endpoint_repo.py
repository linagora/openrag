"""asyncpg-backed :class:`ModelEndpointRepository`.

Manages the ``model_endpoints`` table — named inference endpoint
configurations for embedders, LLMs, rerankers, and VLMs. Phase 14D
replaces the earlier stub with real SQL.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from core.config.model_endpoints import ModelEndpointRow
from core.ports.model_endpoint_repo import ModelEndpointRepository

if TYPE_CHECKING:
    import asyncpg

_ALLOWED_UPDATE_FIELDS = frozenset({"endpoint", "model_name", "batch_size", "timeout", "extra", "is_default"})


class PgModelEndpointRepository(ModelEndpointRepository):
    """asyncpg-backed implementation of :class:`ModelEndpointRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    @staticmethod
    def _to_model(row: asyncpg.Record) -> ModelEndpointRow:
        return ModelEndpointRow(
            name=row["name"],
            model_type=row["model_type"],
            endpoint=row["endpoint"],
            model_name=row["model_name"],
            batch_size=row["batch_size"],
            timeout=row["timeout"],
            extra=row["extra"] or {},
            is_default=row["is_default"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def create(self, row: ModelEndpointRow) -> ModelEndpointRow:
        rec = await self.pool.fetchrow(
            """
            INSERT INTO model_endpoints
                (name, model_type, endpoint, model_name, batch_size, timeout, extra, is_default)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
            RETURNING *
            """,
            row.name,
            row.model_type,
            row.endpoint,
            row.model_name,
            row.batch_size,
            row.timeout,
            row.extra,
            row.is_default,
        )
        return self._to_model(rec)

    async def get(self, name: str, model_type: str) -> ModelEndpointRow | None:
        rec = await self.pool.fetchrow(
            "SELECT * FROM model_endpoints WHERE name = $1 AND model_type = $2",
            name,
            model_type,
        )
        return self._to_model(rec) if rec else None

    async def list_all(self, model_type: str | None = None) -> list[ModelEndpointRow]:
        if model_type is not None:
            rows = await self.pool.fetch(
                "SELECT * FROM model_endpoints WHERE model_type = $1 ORDER BY name",
                model_type,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM model_endpoints ORDER BY model_type, name",
            )
        return [self._to_model(r) for r in rows]

    async def update(self, name: str, model_type: str, **fields: object) -> ModelEndpointRow | None:
        updates = {k: v for k, v in fields.items() if k in _ALLOWED_UPDATE_FIELDS}
        if not updates:
            return await self.get(name, model_type)

        params: list = [name, model_type]
        sets: list[str] = []
        for col, val in updates.items():
            idx = len(params) + 1
            sets.append(f"{col} = ${idx}::jsonb" if col == "extra" else f"{col} = ${idx}")
            params.append(val)

        rec = await self.pool.fetchrow(
            f"UPDATE model_endpoints SET {', '.join(sets)}, updated_at = now() "
            f"WHERE name = $1 AND model_type = $2 RETURNING *",
            *params,
        )
        return self._to_model(rec) if rec else None

    async def rename(self, name: str, model_type: str, new_name: str) -> None:
        await self.pool.execute(
            "UPDATE model_endpoints SET name = $3, updated_at = now() WHERE name = $1 AND model_type = $2",
            name,
            model_type,
            new_name,
        )

    async def delete(self, name: str, model_type: str) -> bool:
        result = await self.pool.execute(
            "DELETE FROM model_endpoints WHERE name = $1 AND model_type = $2",
            name,
            model_type,
        )
        return result == "DELETE 1"

    async def set_default(self, model_type: str, name: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE model_endpoints SET is_default = false, updated_at = now() WHERE model_type = $1",
                    model_type,
                )
                await conn.execute(
                    "UPDATE model_endpoints SET is_default = true, updated_at = now() "
                    "WHERE name = $1 AND model_type = $2",
                    name,
                    model_type,
                )

    async def delete_and_promote_default(self, name: str, model_type: str, promote_to: str | None) -> None:
        # Delete and (when the deleted row was the default) promote a survivor in
        # ONE transaction, so a mid-operation failure can never leave the type with
        # no default endpoint. ``promote_to is None`` => a plain delete.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM model_endpoints WHERE name = $1 AND model_type = $2",
                    name,
                    model_type,
                )
                if promote_to is not None:
                    await conn.execute(
                        "UPDATE model_endpoints SET is_default = false, updated_at = now() WHERE model_type = $1",
                        model_type,
                    )
                    await conn.execute(
                        "UPDATE model_endpoints SET is_default = true, updated_at = now() "
                        "WHERE name = $1 AND model_type = $2",
                        promote_to,
                        model_type,
                    )


__all__ = ["PgModelEndpointRepository"]
