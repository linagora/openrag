"""add model_endpoints.vector_field

Per-embedder dense vector field names (#762 F). Purely additive: the column
lands NULL on every existing row, and NULL is a meaningful value — it means
the endpoint shares the legacy ``vector`` field, which is exactly today's
behaviour. Nothing is backfilled, so an upgrade cannot change how any existing
partition is searched, and no deployment has to re-embed to take it.

Revision ID: a7c3e1d9b482
Revises: 2a3b4c5d6e7f
Create Date: 2026-09-11

"""

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import column_exists, index_exists

revision = "a7c3e1d9b482"
down_revision = "2a3b4c5d6e7f"
branch_labels = None
depends_on = None

_TABLE = "model_endpoints"
_COLUMN = "vector_field"
_INDEX = "uq_model_endpoint_vector_field"


def upgrade() -> None:
    # create_all() runs at startup before alembic, so a fresh database already
    # has both objects — guard each one (see CLAUDE.md, migration idempotency).
    if not column_exists(_TABLE, _COLUMN):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(), nullable=True))
    if not index_exists(_TABLE, _INDEX):
        # Partial: NULL is the legacy shared field and repeats across rows, so
        # only allocated names are constrained to be unique.
        op.create_index(
            _INDEX,
            _TABLE,
            [_COLUMN],
            unique=True,
            postgresql_where=sa.text(f"{_COLUMN} IS NOT NULL"),
        )


def downgrade() -> None:
    if index_exists(_TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if column_exists(_TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
