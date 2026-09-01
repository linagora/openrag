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

import asyncpg
from core.models.prompt import Prompt
from core.ports.prompt_repo import PromptRepository
from core.utils.exceptions import ValidationError

# Only these columns are editable in place. ``is_default`` is excluded on
# purpose: a bare ``UPDATE ... SET is_default = true`` cannot clear the previous
# default in the same statement, so it would collide with the partial unique
# index. Promotion goes through set_default, which clears-then-sets under a lock.
# ``prompt_type`` is immutable — retyping a prompt is nonsensical; create a new
# one instead.
_ALLOWED_UPDATE_FIELDS = frozenset({"name", "content"})

_COLS = ("id", "prompt_type", "name", "content", "is_default", "created_at", "updated_at")
_SELECT_COLS = ", ".join(_COLS)


def _as_conflict(exc: asyncpg.UniqueViolationError, prompt_type: str) -> ValidationError:
    """Translate a unique-index violation into the 409 the service intends.

    The service checks for a name clash before writing, but that check and the
    write are not one atomic step: two concurrent admins creating (or renaming
    to) the same name both pass it, and the loser hits the index. Without this
    the loser gets a 500 from the generic exception handler instead of the same
    409 the sequential path returns. Mirrors PgPartitionRepository.create.
    """
    if exc.constraint_name == "uix_prompts_default_per_type":
        return ValidationError(
            f"Another '{prompt_type}' prompt was made the default concurrently; retry.",
            status_code=409,
            code="PROMPT_DEFAULT_CONFLICT",
        )
    return ValidationError(
        f"A '{prompt_type}' prompt with that name already exists.",
        status_code=409,
        code="PROMPT_EXISTS",
    )


# Indexation/retrieval preset config field -> the prompt_type it names. Partition
# prompt-map keys (final-answer prompts) ARE prompt_type values, so they need no
# mapping.
_PRESET_FIELD_TO_TYPE = {
    "contextualization_prompt_name": "chunk_contextualizer",
    "asr_transcription_prompt_name": "asr_transcription",
    "image_captioning_prompt_name": "image_captioning",
    "topic_tagging_prompt_name": "topic_tagger",
    "hyde_prompt_name": "hyde",
    "multi_query_prompt_name": "multi_query",
    "query_contextualizer_prompt_name": "query_contextualizer",
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
                try:
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
                except asyncpg.UniqueViolationError as exc:
                    raise _as_conflict(exc, prompt.prompt_type) from exc
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

        try:
            rec = await self.pool.fetchrow(
                f"UPDATE prompts SET {', '.join(sets)}, updated_at = now() WHERE id = $1 RETURNING {_SELECT_COLS}",
                *params,
            )
        except asyncpg.UniqueViolationError as exc:
            # A rename racing another rename/create onto the same name.
            existing = await self.get(prompt_id)
            raise _as_conflict(exc, existing.prompt_type if existing else "") from exc
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
        # Effective resolution count: every partition resolves each prompt type to
        # a named library prompt (when its partition/preset config names an
        # existing one) or, failing that, to the type's global default. We count
        # that resolution — so a default correctly shows the partitions that fall
        # back to it, not just the (usually zero) partitions that name it
        # explicitly. Per type the counts sum to the partition total.
        total_partitions = await self.pool.fetchval("SELECT count(*)::int FROM partitions")

        prompt_rows = await self.pool.fetch("SELECT prompt_type, name, is_default FROM prompts")
        existing = {(r["prompt_type"], r["name"]) for r in prompt_rows}
        default_name: dict[str, str] = {r["prompt_type"]: r["name"] for r in prompt_rows if r["is_default"]}

        # Explicit overrides: partitions that name a final-answer prompt
        # directly, or transitively through their active indexation/retrieval
        # preset's ``*_prompt_name`` setting.
        overrides: dict[tuple[str, str], int] = {}
        part_rows = await self.pool.fetch(
            """
            SELECT j.key AS prompt_type, j.value AS name, count(*)::int AS n
            FROM partitions p, jsonb_each_text(p.generation_prompt_names) j
            WHERE j.value <> ''
            GROUP BY 1, 2
            """
        )
        for r in part_rows:
            overrides[(r["prompt_type"], r["name"])] = overrides.get((r["prompt_type"], r["name"]), 0) + r["n"]
        # count(DISTINCT partition) so a partition is counted once per prompt even
        # if two of its presets happened to name it.
        preset_rows = await self.pool.fetch(
            """
            SELECT c.key AS field, c.value AS name, count(DISTINCT part.partition)::int AS n
            FROM partitions part
            JOIN pipeline_presets pre
              ON (pre.preset_type = 'indexation' AND pre.name = part.indexation_preset)
              OR (pre.preset_type = 'retrieval'  AND pre.name = part.retrieval_preset)
            CROSS JOIN LATERAL jsonb_each_text(pre.config) c
            WHERE c.value <> ''
            GROUP BY 1, 2
            """
        )
        for r in preset_rows:
            prompt_type = _PRESET_FIELD_TO_TYPE.get(r["field"])
            if prompt_type:
                key = (prompt_type, r["name"])
                overrides[key] = overrides.get(key, 0) + r["n"]

        # A valid override (names an existing prompt) credits that prompt; a
        # dangling one falls through. Each type's default then absorbs every
        # partition that didn't validly override it.
        counts: dict[tuple[str, str], int] = {}
        valid_overrides: dict[str, int] = {}
        for (prompt_type, name), n in overrides.items():
            if (prompt_type, name) in existing:
                counts[(prompt_type, name)] = counts.get((prompt_type, name), 0) + n
                valid_overrides[prompt_type] = valid_overrides.get(prompt_type, 0) + n
        for prompt_type, d_name in default_name.items():
            fallback = max(0, (total_partitions or 0) - valid_overrides.get(prompt_type, 0))
            if fallback:
                counts[(prompt_type, d_name)] = counts.get((prompt_type, d_name), 0) + fallback
        return counts


__all__ = ["PgPromptRepository"]
