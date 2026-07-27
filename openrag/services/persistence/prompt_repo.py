"""asyncpg-backed :class:`PromptRepository`.

Manages the ``prompts`` library table. Replaces the earlier stub. Effective
resolution (named prompt → default → disk seed) is the service's job; this
layer is pure storage plus the one invariant that needs SQL-level atomicity:
at most one ``is_default`` row per type — held by a partial unique index and
enforced by clear-then-set inside a locked transaction (:meth:`set_default`,
and the default branch of :meth:`create`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from core.models.prompt import Prompt
from core.ports.prompt_repo import PromptRepository

if TYPE_CHECKING:
    import asyncpg

# Only these columns are editable in place. ``is_default`` is excluded on
# purpose: a bare ``UPDATE ... SET is_default = true`` cannot clear the previous
# default in the same statement, so it would collide with the partial unique
# index. Promotion goes through set_default, which clears-then-sets under a lock.
# ``prompt_type`` is immutable — retyping a prompt is nonsensical; create a new
# one instead.
_ALLOWED_UPDATE_FIELDS = frozenset({"name", "content"})

_COLS = ("id", "prompt_type", "name", "content", "is_default", "created_at", "updated_at")
_SELECT_COLS = ", ".join(_COLS)

# Indexation/retrieval preset config field -> the prompt_type it names. Partition
# generation_prompt_names keys ARE prompt_type values, so they need no mapping.
_PRESET_FIELD_TO_TYPE = {
    "contextualization_prompt_name": "chunk_contextualizer",
    "image_captioning_prompt_name": "image_captioning",
    "topic_tagging_prompt_name": "topic_tagger",
    "hyde_prompt_name": "hyde",
    "multi_query_prompt_name": "multi_query",
}


class PgPromptRepository(PromptRepository):
    """asyncpg-backed implementation of :class:`PromptRepository`."""

    def __init__(self, pool_getter: Callable[[], asyncpg.Pool]) -> None:
        self._pool_getter = pool_getter

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    @staticmethod
    def _to_model(row: asyncpg.Record) -> Prompt:
        return Prompt(
            id=row["id"],
            prompt_type=row["prompt_type"],
            name=row["name"],
            content=row["content"],
            is_default=row["is_default"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Library CRUD
    # ------------------------------------------------------------------

    async def create(self, prompt: Prompt) -> Prompt:
        # A bare INSERT with is_default=true would collide with the partial
        # unique index if a default already exists for the type, so demote the
        # current default in the SAME transaction as the insert — the new prompt
        # becomes the sole default atomically.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if prompt.is_default:
                    await conn.execute(
                        "UPDATE prompts SET is_default = false, updated_at = now() "
                        "WHERE prompt_type = $1 AND is_default = true",
                        prompt.prompt_type,
                    )
                rec = await conn.fetchrow(
                    f"""
                    INSERT INTO prompts (id, prompt_type, name, content, is_default)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING {_SELECT_COLS}
                    """,
                    prompt.id,
                    prompt.prompt_type,
                    prompt.name,
                    prompt.content,
                    prompt.is_default,
                )
        return self._to_model(rec)

    async def get(self, prompt_id: str) -> Prompt | None:
        rec = await self.pool.fetchrow(
            f"SELECT {_SELECT_COLS} FROM prompts WHERE id = $1",
            prompt_id,
        )
        return self._to_model(rec) if rec else None

    async def list(
        self,
        *,
        prompt_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Prompt]:
        if prompt_type is not None:
            rows = await self.pool.fetch(
                f"SELECT {_SELECT_COLS} FROM prompts WHERE prompt_type = $1 "
                "ORDER BY prompt_type, name, created_at OFFSET $2 LIMIT $3",
                prompt_type,
                offset,
                limit,
            )
        else:
            rows = await self.pool.fetch(
                f"SELECT {_SELECT_COLS} FROM prompts ORDER BY prompt_type, name, created_at OFFSET $1 LIMIT $2",
                offset,
                limit,
            )
        return [self._to_model(r) for r in rows]

    async def count(self, *, prompt_type: str | None = None) -> int:
        if prompt_type is not None:
            return await self.pool.fetchval(
                "SELECT count(*) FROM prompts WHERE prompt_type = $1",
                prompt_type,
            )
        return await self.pool.fetchval("SELECT count(*) FROM prompts")

    async def update(self, prompt_id: str, **fields: object) -> Prompt | None:
        updates = {k: v for k, v in fields.items() if k in _ALLOWED_UPDATE_FIELDS}
        if not updates:
            return await self.get(prompt_id)

        params: list = [prompt_id]
        sets: list[str] = []
        for col, val in updates.items():
            params.append(val)
            sets.append(f"{col} = ${len(params)}")

        rec = await self.pool.fetchrow(
            f"UPDATE prompts SET {', '.join(sets)}, updated_at = now() WHERE id = $1 RETURNING {_SELECT_COLS}",
            *params,
        )
        return self._to_model(rec) if rec else None

    async def delete(self, prompt_id: str) -> bool:
        # Presets/partitions reference prompts by *name* (soft refs in JSONB), so
        # there is no FK cascade: a deleted prompt's stale references simply
        # resolve to the global default. The service guards against deleting a
        # default; callers surface usage counts before offering delete.
        result = await self.pool.execute("DELETE FROM prompts WHERE id = $1", prompt_id)
        return result == "DELETE 1"

    # ------------------------------------------------------------------
    # Global default (one per type)
    # ------------------------------------------------------------------

    async def get_by_name(self, prompt_type: str, name: str) -> Prompt | None:
        rec = await self.pool.fetchrow(
            f"SELECT {_SELECT_COLS} FROM prompts WHERE prompt_type = $1 AND name = $2",
            prompt_type,
            name,
        )
        return self._to_model(rec) if rec else None

    async def get_default(self, prompt_type: str) -> Prompt | None:
        rec = await self.pool.fetchrow(
            f"SELECT {_SELECT_COLS} FROM prompts WHERE prompt_type = $1 AND is_default = true",
            prompt_type,
        )
        return self._to_model(rec) if rec else None

    async def set_default(self, prompt_id: str) -> Prompt | None:
        """Promote ``prompt_id`` to the default for its type, atomically.

        Locks the type's rows (FOR UPDATE) and confirms ``prompt_id`` still
        exists *inside* the transaction before clearing the old default, so a
        concurrent delete of ``prompt_id`` can't make the final UPDATE match 0
        rows after the previous default was already cleared — which would leave
        the type with no default. Same invariant PgModelEndpointRepository
        protects.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow("SELECT prompt_type FROM prompts WHERE id = $1", prompt_id)
                if target is None:
                    return None
                prompt_type = target["prompt_type"]
                locked = await conn.fetch(
                    "SELECT id FROM prompts WHERE prompt_type = $1 FOR UPDATE",
                    prompt_type,
                )
                if prompt_id not in {r["id"] for r in locked}:
                    return None
                await conn.execute(
                    "UPDATE prompts SET is_default = false, updated_at = now() "
                    "WHERE prompt_type = $1 AND is_default = true",
                    prompt_type,
                )
                rec = await conn.fetchrow(
                    f"UPDATE prompts SET is_default = true, updated_at = now() WHERE id = $1 RETURNING {_SELECT_COLS}",
                    prompt_id,
                )
        return self._to_model(rec)

    async def reference_counts(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        # Partition generation prompts: the JSONB keys ARE prompt_type values.
        part_rows = await self.pool.fetch(
            """
            SELECT j.key AS prompt_type, j.value AS name, count(*)::int AS n
            FROM partitions p, jsonb_each_text(p.generation_prompt_names) j
            GROUP BY 1, 2
            """
        )
        for r in part_rows:
            counts[(r["prompt_type"], r["name"])] = r["n"]
        # Preset *_prompt_name config fields, mapped to their prompt_type.
        preset_rows = await self.pool.fetch(
            """
            SELECT c.key AS field, c.value AS name, count(*)::int AS n
            FROM pipeline_presets p, jsonb_each_text(p.config) c
            GROUP BY 1, 2
            """
        )
        for r in preset_rows:
            prompt_type = _PRESET_FIELD_TO_TYPE.get(r["field"])
            if prompt_type and r["name"]:
                key = (prompt_type, r["name"])
                counts[key] = counts.get(key, 0) + r["n"]
        return counts


__all__ = ["PgPromptRepository"]
