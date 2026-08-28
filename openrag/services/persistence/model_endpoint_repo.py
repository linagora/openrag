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
from core.utils.exceptions import NotFoundError

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

# Endpoint names are referenced by value elsewhere, and nothing updates those
# references when an endpoint is renamed (#770) — so ``rename()`` cascades to
# every known reference in the same transaction as the name change. Direct
# endpoint-name columns on ``partitions``, keyed by the model_type they hold:
_PARTITION_COLUMN_BY_TYPE = {"embedder": "embedder", "llm": "chat_llm"}
# Endpoint-name keys embedded in ``pipeline_presets.config`` (JSONB), by the
# preset_type that carries them — see core/config/retrieval_pipeline.py and
# core/config/indexation_pipeline.py for the field definitions.
_RETRIEVAL_PRESET_KEYS_BY_TYPE = {"llm": ("llm",), "reranker": ("reranker",)}
_INDEXATION_PRESET_KEYS_BY_TYPE = {
    "llm": ("contextualization_llm", "metadata_extraction_llm", "topic_tagging_llm"),
    "vlm": ("vlm",),
    "stt": ("stt",),
}


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
        # A bare INSERT with is_default=true cannot clear the previous default, so
        # POST /model-endpoints/ {"is_default": true} would leave two is_default=true
        # rows for one model_type (same hazard the update path avoids by routing
        # through set_default). Demote any existing default in the SAME transaction
        # as the insert so the new endpoint becomes the sole default atomically.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if row.is_default:
                    await conn.execute(
                        "UPDATE model_endpoints SET is_default = false, updated_at = now() WHERE model_type = $1",
                        row.model_type,
                    )
                rec = await conn.fetchrow(
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
        """Rename an endpoint and cascade the new name to every stored reference.

        Left alone, a rename silently strands every partition or preset that
        pointed at the old name — ``partitions.embedder`` / ``partitions.chat_llm``,
        and the endpoint-name fields embedded in ``pipeline_presets.config``
        (JSONB) — since nothing else in the schema updates those when the
        referenced row's name changes (#770). All writes run in this one
        transaction so a partial cascade can never leave the registry and its
        referents disagreeing.

        The caller (``ModelEndpointService.update_model_endpoint``) still owns
        refreshing the in-memory partition/preset caches afterwards — this
        method only makes the DB-side references consistent.

        Raises :class:`NotFoundError` if ``name`` vanished between the
        service's existence check and this transaction (a concurrent delete)
        — mirroring ``PgPipelinePresetRepository.rename``. Without the
        ``RETURNING`` check, a lost race would still run the cascade below,
        repointing partitions/presets at a ``new_name`` that was never
        actually created.

        Also ``LOCK``s ``partitions`` ``IN SHARE MODE`` before touching
        anything — the same lock :meth:`PgPresetRepository.delete` takes, and
        the same table :meth:`PgPartitionRepository.update_partition` writes
        to *before* its own DB-authoritative ``chat_llm`` re-check. Without
        this, a partition PATCH could validate ``name`` against the in-memory
        catalog, then block behind this transaction's cascade on the exact
        row it's about to write, and resume writing the now-renamed-away
        ``name`` straight back once this commits — permanently stranding that
        partition the moment the service's temporary alias (see
        ``ModelEndpointService._alias_renamed_name``) drops. Locking first, in
        the same order both call sites use, makes the two block on each other
        instead of interleaving: whichever transaction's ``partitions`` write
        commits first is the one the other observes.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("LOCK TABLE partitions IN SHARE MODE")

                renamed = await conn.fetchrow(
                    "UPDATE model_endpoints SET name = $3, updated_at = now() "
                    "WHERE name = $1 AND model_type = $2 RETURNING name",
                    name,
                    model_type,
                    new_name,
                )
                if renamed is None:
                    raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")

                partition_col = _PARTITION_COLUMN_BY_TYPE.get(model_type)
                if partition_col:
                    await conn.execute(
                        f"UPDATE partitions SET {partition_col} = $2 WHERE {partition_col} = $1",
                        name,
                        new_name,
                    )

                for preset_type, keys in (
                    ("retrieval", _RETRIEVAL_PRESET_KEYS_BY_TYPE.get(model_type, ())),
                    ("indexation", _INDEXATION_PRESET_KEYS_BY_TYPE.get(model_type, ())),
                ):
                    for key in keys:
                        await conn.execute(
                            """
                            UPDATE pipeline_presets
                            SET config = jsonb_set(config, $1::text[], to_jsonb($2::text)), updated_at = now()
                            WHERE preset_type = $3 AND config->>$4 = $5
                            """,
                            [key],
                            new_name,
                            preset_type,
                            key,
                            name,
                        )

    async def delete(self, name: str, model_type: str) -> bool:
        result = await self.pool.execute(
            "DELETE FROM model_endpoints WHERE name = $1 AND model_type = $2",
            name,
            model_type,
        )
        return result == "DELETE 1"

    async def set_default(self, model_type: str, name: str) -> None:
        """Promote ``name`` to the default for ``model_type``, atomically.

        Locks the type's rows (FOR UPDATE) and confirms ``name`` still exists
        *inside* the transaction before clearing the old default. Without the
        lock + existence check, a concurrent delete of ``name`` between the
        caller's existence check and this transaction would make the second
        UPDATE match 0 rows AFTER the first already cleared the previous default
        — leaving the type with no default at all. Same invariant
        ``delete_and_promote_default`` protects. Raises ``NotFoundError`` if the
        target endpoint is gone.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    "SELECT name FROM model_endpoints WHERE model_type = $1 FOR UPDATE",
                    model_type,
                )
                if name not in {r["name"] for r in rows}:
                    raise NotFoundError(f"Endpoint '{name}' of type '{model_type}' not found.")
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
        """Delete an endpoint and, if it was the default, promote a survivor to
        default — all atomically and decided under a row lock.

        Locking and deciding inside one transaction means concurrent deletes of the
        same model type can't both pass a stale last-endpoint check or promote an
        already-deleted survivor, so the type is never left with no endpoint or no
        default. Returns ``(status, promoted_name)`` where ``status`` is
        ``"not_found" | "last" | "ok"`` and ``promoted_name`` is set only when a
        deleted default was replaced.
        """
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
