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

# 'is_default' is deliberately excluded: a bare ``UPDATE ... SET is_default = true``
# cannot clear the previous default in the same statement, so it would leave two
# is_default=true rows for one model_type — and load_all() then resolves the
# 'default' alias to whichever endpoint sorts last by name, not the one the caller
# picked. (Verified against the live admin API: a single PUT of {"is_default": true}
# on a non-default endpoint yielded two defaults for the type.) Promotion must go
# through set_default / delete_and_promote_default, which clear-then-set inside one
# transaction; ModelEndpointService.update_model_endpoint routes is_default there.
_ALLOWED_UPDATE_FIELDS = frozenset({"endpoint", "model_name", "batch_size", "timeout", "extra"})


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

    async def delete_and_promote_default(self, name: str, model_type: str) -> tuple[str, str | None]:
        # Lock every row of this model_type (FOR UPDATE) so concurrent deletes of
        # the same type serialize, then make the last-endpoint guard and survivor
        # choice from the LOCKED, current state — not a stale snapshot. Otherwise two
        # concurrent deletes could each pass a snapshot count check and remove the
        # last row, or promote a survivor that another tx just deleted, leaving the
        # type with no endpoint / no default. Returns (status, promoted_name):
        # status is "not_found" | "last" | "ok"; promoted_name is set only when the
        # deleted row was the default and a survivor was promoted.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "SELECT name, is_default FROM model_endpoints WHERE model_type = $1 ORDER BY name FOR UPDATE",
                    model_type,
                )
                names = [r["name"] for r in rows]
                if name not in names:
                    return ("not_found", None)
                if len(names) <= 1:
                    return ("last", None)
                was_default = next(r["is_default"] for r in rows if r["name"] == name)
                await conn.execute(
                    "DELETE FROM model_endpoints WHERE name = $1 AND model_type = $2",
                    name,
                    model_type,
                )
                promoted: str | None = None
                if was_default:
                    promoted = next(n for n in names if n != name)  # deterministic: first by name
                    await conn.execute(
                        "UPDATE model_endpoints SET is_default = false, updated_at = now() WHERE model_type = $1",
                        model_type,
                    )
                    await conn.execute(
                        "UPDATE model_endpoints SET is_default = true, updated_at = now() "
                        "WHERE name = $1 AND model_type = $2",
                        promoted,
                        model_type,
                    )
                return ("ok", promoted)


__all__ = ["PgModelEndpointRepository"]
