"""give every embedder its own vector field

Adds ``model_endpoints.vector_field`` and allocates a name for each existing
embedder. The Milvus schema-v3 migration then moves each partition's vectors
into its embedder's field, reading the names allocated here.

Partitions on the ``default`` alias that hold files are pinned to the embedder
the alias resolves to now, since their vectors go to that embedder's field.
Empty ones keep the alias.

Revision ID: c4e8f2a6b913
Revises: 3b4c5d6e7f8a
Create Date: 2026-09-13

"""

import re
from collections.abc import Collection

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import (
    check_constraint_exists,
    column_exists,
    index_exists,
)

revision = "c4e8f2a6b913"
down_revision = "3b4c5d6e7f8a"
branch_labels = None
depends_on = None

_ENDPOINTS = "model_endpoints"
_PARTITIONS = "partitions"
_COLUMN = "vector_field"
_INDEX = "uq_model_endpoint_vector_field"
_CONSTRAINT = "ck_embedder_has_vector_field"
_DEFAULT_ALIAS = "default"
_HOLDS_FILES = "EXISTS (SELECT 1 FROM files f WHERE f.partition_name = p.partition)"

# The application's allocator as of this revision, copied so that the names
# this migration produces never change.
_DISALLOWED = re.compile(r"[^0-9A-Za-z_]+")
_UNDERSCORE_RUNS = re.compile(r"_{2,}")
_MAX_LENGTH = 255


def _allocate(endpoint_name: str, taken: Collection[str]) -> str:
    stem = _UNDERSCORE_RUNS.sub("_", _DISALLOWED.sub("_", endpoint_name)).strip("_") or "embedder"
    candidate = ("vector_" + stem)[:_MAX_LENGTH]
    attempt, n = candidate, 1
    while attempt in taken:
        n += 1
        suffix = f"_{n}"
        attempt = candidate[: _MAX_LENGTH - len(suffix)] + suffix
    return attempt


def _backfill_vector_fields(conn: sa.engine.Connection) -> None:
    taken = set(conn.execute(sa.text(f"SELECT {_COLUMN} FROM {_ENDPOINTS} WHERE {_COLUMN} IS NOT NULL")).scalars())
    names = conn.execute(
        sa.text(
            f"SELECT name FROM {_ENDPOINTS} WHERE model_type = 'embedder' AND {_COLUMN} IS NULL "
            "ORDER BY created_at, name"
        )
    ).scalars()
    for name in list(names):
        field = _allocate(name, taken)
        conn.execute(
            sa.text(f"UPDATE {_ENDPOINTS} SET {_COLUMN} = :field WHERE model_type = 'embedder' AND name = :name"),
            {"field": field, "name": name},
        )
        taken.add(field)


def _pin_default_alias(conn: sa.engine.Connection) -> None:
    defaults = list(
        conn.execute(sa.text(f"SELECT name FROM {_ENDPOINTS} WHERE model_type = 'embedder' AND is_default")).scalars()
    )
    if len(defaults) != 1:
        riding = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {_PARTITIONS} p WHERE p.embedder = :alias AND {_HOLDS_FILES}"),
            {"alias": _DEFAULT_ALIAS},
        ).scalar_one()
        if not riding:
            return
        raise RuntimeError(
            f"{riding} partition(s) holding files use the '{_DEFAULT_ALIAS}' embedder alias, but there are "
            f"{len(defaults)} default embedder endpoints, so the alias cannot be pinned to one embedder. "
            "Mark exactly one embedder endpoint as the default, then run the migrations again."
        )
    conn.execute(
        sa.text(
            f"UPDATE {_PARTITIONS} p SET embedder = :name, updated_at = now() "
            f"WHERE p.embedder = :alias AND {_HOLDS_FILES}"
        ),
        {"name": defaults[0], "alias": _DEFAULT_ALIAS},
    )


def upgrade() -> None:
    # create_all() runs at startup before alembic, so on a fresh database the
    # column, index and constraint already exist — guard each one.
    if not column_exists(_ENDPOINTS, _COLUMN):
        op.add_column(_ENDPOINTS, sa.Column(_COLUMN, sa.String(), nullable=True))
    if not index_exists(_ENDPOINTS, _INDEX):
        op.create_index(_INDEX, _ENDPOINTS, [_COLUMN], unique=True, postgresql_where=sa.text(f"{_COLUMN} IS NOT NULL"))
    conn = op.get_bind()
    _backfill_vector_fields(conn)
    if column_exists(_PARTITIONS, "embedder"):
        _pin_default_alias(conn)
    if not check_constraint_exists(_ENDPOINTS, _CONSTRAINT):
        op.create_check_constraint(_CONSTRAINT, _ENDPOINTS, f"model_type <> 'embedder' OR {_COLUMN} IS NOT NULL")


def downgrade() -> None:
    # Pinned partitions stay pinned: they name the embedder the alias resolved to.
    if check_constraint_exists(_ENDPOINTS, _CONSTRAINT):
        op.drop_constraint(_CONSTRAINT, _ENDPOINTS, type_="check")
    if index_exists(_ENDPOINTS, _INDEX):
        op.drop_index(_INDEX, table_name=_ENDPOINTS)
    if column_exists(_ENDPOINTS, _COLUMN):
        op.drop_column(_ENDPOINTS, _COLUMN)
