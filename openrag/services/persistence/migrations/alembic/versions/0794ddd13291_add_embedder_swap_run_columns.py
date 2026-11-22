"""add run_id and chunks_over_window to partition_embedder_swaps

``run_id`` scopes a runner's writes to the swap it was started for;
``chunks_over_window`` counts the chunks cut to the target's window. A database
that ran ``e1f3a5c7d9b2`` already has the table without them; create_all() gives
a fresh one both, so each is added only when missing.

Revision ID: 0794ddd13291
Revises: e1f3a5c7d9b2
Create Date: 2026-10-07

"""

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import column_exists, table_exists

revision = "0794ddd13291"
down_revision = "e1f3a5c7d9b2"
branch_labels = None
depends_on = None

_TABLE = "partition_embedder_swaps"


def upgrade() -> None:
    if not table_exists(_TABLE):
        return
    if not column_exists(_TABLE, "chunks_over_window"):
        op.add_column(_TABLE, sa.Column("chunks_over_window", sa.Integer(), server_default="0", nullable=False))
    if not column_exists(_TABLE, "run_id"):
        # Existing rows get an empty run, which their resumed runner then writes to.
        op.add_column(_TABLE, sa.Column("run_id", sa.String(), server_default="", nullable=False))
        op.alter_column(_TABLE, "run_id", server_default=None)


def downgrade() -> None:
    if not table_exists(_TABLE):
        return
    for column in ("run_id", "chunks_over_window"):
        if column_exists(_TABLE, column):
            op.drop_column(_TABLE, column)
