"""backfill model_endpoints.vector_field for every embedder

The shared ``vector`` field is going away (#762 F): every embedder reads and
writes a dense field of its own, including the ones that predate
per-embedder fields and still carry ``vector_field = NULL``. This revision
gives each of those a name, then makes a missing name impossible.

It only moves metadata. The vectors themselves still sit in ``vector`` until
the Milvus schema-v3 migration copies them into these fields — which is why
that migration reads the names allocated here instead of computing its own.

Two data changes and one constraint:

1. **Allocate a field for each legacy embedder**, oldest first, with the same
   rules the repository applies on create. The allocator is copied below
   rather than imported: a migration must keep producing the names it produced
   the day it shipped, whatever later happens to the application code.
2. **Replace the ``default`` alias on partitions with the embedder it resolves
   to today.** A partition on the alias follows every set-default. With one
   shared field that silently mixed vector spaces; with a field per embedder it
   would point the partition at a field its rows were never copied into. The
   Milvus migration routes a partition's rows by this column, so it has to name
   a real embedder first.
3. **``ck_embedder_has_vector_field``** — a check constraint rather than
   ``NOT NULL``, because the same table holds LLM, reranker, VLM and STT rows,
   which never own a field.

Revision ID: c4e8f2a6b913
Revises: a7c3e1d9b482
Create Date: 2026-09-13

"""

import re
from collections.abc import Collection

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import (
    check_constraint_exists,
    column_exists,
    table_exists,
)

revision = "c4e8f2a6b913"
down_revision = "a7c3e1d9b482"
branch_labels = None
depends_on = None

_ENDPOINTS = "model_endpoints"
_PARTITIONS = "partitions"
_CONSTRAINT = "ck_embedder_has_vector_field"
_CONSTRAINT_SQL = "model_type <> 'embedder' OR vector_field IS NOT NULL"
_DEFAULT_ALIAS = "default"

# Frozen copy of core.vector_stores.vector_field as of this revision.
_DISALLOWED = re.compile(r"[^0-9A-Za-z_]+")
_UNDERSCORE_RUNS = re.compile(r"_{2,}")
_PREFIX = "vector_"
_LEGACY_FIELD = "vector"
_MAX_LENGTH = 255
_FALLBACK_STEM = "embedder"


def _allocate(endpoint_name: str, taken: Collection[str]) -> str:
    reserved = {_LEGACY_FIELD, *taken}
    stem = _UNDERSCORE_RUNS.sub("_", _DISALLOWED.sub("_", endpoint_name)).strip("_") or _FALLBACK_STEM
    candidate = (_PREFIX + stem)[:_MAX_LENGTH]
    if candidate not in reserved:
        return candidate
    for suffix_n in range(2, len(reserved) + 3):
        suffix = f"_{suffix_n}"
        attempt = candidate[: _MAX_LENGTH - len(suffix)] + suffix
        if attempt not in reserved:
            return attempt
    raise RuntimeError(f"Could not allocate a vector field name for '{endpoint_name}'.")


def _backfill_vector_fields(conn: sa.engine.Connection) -> None:
    taken = set(
        conn.execute(sa.text(f"SELECT vector_field FROM {_ENDPOINTS} WHERE vector_field IS NOT NULL")).scalars()
    )
    legacy = conn.execute(
        sa.text(
            f"SELECT name FROM {_ENDPOINTS} "
            "WHERE model_type = 'embedder' AND vector_field IS NULL "
            "ORDER BY created_at, name"
        )
    ).scalars()
    for name in list(legacy):
        field = _allocate(name, taken)
        conn.execute(
            sa.text(f"UPDATE {_ENDPOINTS} SET vector_field = :field WHERE model_type = 'embedder' AND name = :name"),
            {"field": field, "name": name},
        )
        taken.add(field)


def _pin_default_alias(conn: sa.engine.Connection) -> None:
    defaults = list(
        conn.execute(sa.text(f"SELECT name FROM {_ENDPOINTS} WHERE model_type = 'embedder' AND is_default")).scalars()
    )
    if len(defaults) != 1:
        # No default means the alias resolves to nothing; several make the
        # choice a guess. Either way the partitions riding it cannot be routed
        # to a dense field, so stop rather than migrate into that state — the
        # operator sets a default and runs this again. Nobody on the alias,
        # nothing to pin.
        riding = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {_PARTITIONS} WHERE embedder = :alias"), {"alias": _DEFAULT_ALIAS}
        ).scalar_one()
        if not riding:
            return
        raise RuntimeError(
            f"{riding} partition(s) use the '{_DEFAULT_ALIAS}' embedder alias, and this deployment has "
            f"{len(defaults)} default embedder endpoints. Each embedder now owns its own vector field "
            "(#762), so the alias has to be pinned to one concrete embedder before those partitions can "
            "be read or written. Mark exactly one embedder endpoint as the default, then run the "
            "migrations again."
        )
    conn.execute(
        sa.text(f"UPDATE {_PARTITIONS} SET embedder = :name WHERE embedder = :alias"),
        {"name": defaults[0], "alias": _DEFAULT_ALIAS},
    )


def upgrade() -> None:
    # create_all() runs at startup before alembic, so on a fresh database the
    # constraint already exists and no embedder is NULL — every step is a no-op.
    if not column_exists(_ENDPOINTS, "vector_field"):
        return
    conn = op.get_bind()
    _backfill_vector_fields(conn)
    if column_exists(_PARTITIONS, "embedder"):
        _pin_default_alias(conn)
    if not check_constraint_exists(_ENDPOINTS, _CONSTRAINT):
        op.create_check_constraint(_CONSTRAINT, _ENDPOINTS, _CONSTRAINT_SQL)


def downgrade() -> None:
    # Allocated names and pinned partitions are kept: the names are dropped
    # with the column by a7c3e1d9b482's downgrade, and a pinned partition names
    # the same embedder the alias resolved to when it was pinned.
    if table_exists(_ENDPOINTS) and check_constraint_exists(_ENDPOINTS, _CONSTRAINT):
        op.drop_constraint(_CONSTRAINT, _ENDPOINTS, type_="check")
