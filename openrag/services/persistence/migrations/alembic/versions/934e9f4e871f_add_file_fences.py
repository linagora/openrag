"""add file_fences

One row per file an operation is changing right now: indexing it, replacing
it, deleting it or updating its metadata (#1430). Nothing writes it yet.

Revision ID: 934e9f4e871f
Revises: f3a4b5c6d7e8
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists

revision: str = "934e9f4e871f"
down_revision: str | Sequence[str] | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "file_fences"


def upgrade() -> None:
    # Guarded: ``metadata.create_all()`` runs at startup and may have created
    # the table before alembic reaches this revision.
    if table_exists(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("partition_name", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("holder_kind", sa.String(), nullable=False),
        sa.Column("holder_id", sa.String(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("partition_name", "file_id"),
        # Unnamed, so this and ``create_all`` converge on the Postgres default
        # names.
        sa.ForeignKeyConstraint(["partition_name"], ["partitions.partition"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "holder_kind IN ('index','delete','metadata')",
            name="ck_file_fences_holder_kind",
        ),
    )


def downgrade() -> None:
    if table_exists(_TABLE):
        op.drop_table(_TABLE)
