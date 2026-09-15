"""add partition_embedder_swaps

State of a partition's embedder swap (#762 F4): its chunks are re-embedded in
place into the new embedder's vector field while the old one keeps serving, and
the partition is locked against writes until it completes. One row per
partition — the running swap, or how the last one ended.

Revision ID: e1f3a5c7d9b2
Revises: c4e8f2a6b913
Create Date: 2026-09-14

"""

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import index_exists, table_exists

revision = "e1f3a5c7d9b2"
down_revision = "c4e8f2a6b913"
branch_labels = None
depends_on = None

_TABLE = "partition_embedder_swaps"
_INDEXES = {
    "ix_partition_embedder_swaps_status": "status",
    "ix_partition_embedder_swaps_target_embedder": "target_embedder",
}


def upgrade() -> None:
    # create_all() runs at startup before alembic, so a fresh database already
    # has the table — guard each object (see CLAUDE.md, migration idempotency).
    if not table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("partition", sa.String(), primary_key=True),
            sa.Column("source_embedder", sa.String(), nullable=False),
            sa.Column("target_embedder", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("files_total", sa.Integer(), server_default="0", nullable=False),
            sa.Column("files_done", sa.Integer(), server_default="0", nullable=False),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["partition"], ["partitions.partition"], ondelete="CASCADE"),
        )
    for index, column in _INDEXES.items():
        if not index_exists(_TABLE, index):
            op.create_index(index, _TABLE, [column])


def downgrade() -> None:
    for index in _INDEXES:
        if index_exists(_TABLE, index):
            op.drop_index(index, table_name=_TABLE)
    if table_exists(_TABLE):
        op.drop_table(_TABLE)
