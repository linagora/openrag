"""add rag audit runs

Revision ID: b7c8d9e0f1a2
Revises: f5b6c918f741
Create Date: 2026-05-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, index_exists, table_exists

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "f5b6c918f741"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Idempotent because Base.metadata.create_all() may have already created the
    table from SQLAlchemy models before Alembic is run.
    """
    if not table_exists("rag_audit_runs"):
        op.create_table(
            "rag_audit_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.String(), nullable=False, unique=True),
            sa.Column("partition_id", sa.Integer(), nullable=True),
            sa.Column(
                "partition_name",
                sa.String(),
                sa.ForeignKey("partitions.partition", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("document_count", sa.Integer(), nullable=True),
            sa.Column("chunk_count", sa.Integer(), nullable=True),
            sa.Column("overall_score", sa.Float(), nullable=True),
            sa.Column("overall_grade", sa.String(), nullable=True),
            sa.Column("config_json", sa.JSON(), nullable=True),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("error", sa.String(), nullable=True),
        )
    elif not column_exists("rag_audit_runs", "partition_id"):
        op.add_column("rag_audit_runs", sa.Column("partition_id", sa.Integer(), nullable=True))

    if column_exists("rag_audit_runs", "partition_id"):
        op.execute(
            """
            UPDATE rag_audit_runs
            SET partition_id = (
                SELECT partitions.id
                FROM partitions
                WHERE partitions.partition = rag_audit_runs.partition_name
            )
            WHERE partition_id IS NULL
            """
        )

    if not index_exists("rag_audit_runs", "ix_rag_audit_runs_run_id"):
        op.create_index("ix_rag_audit_runs_run_id", "rag_audit_runs", ["run_id"])
    if not index_exists("rag_audit_runs", "ix_rag_audit_runs_partition_id"):
        op.create_index("ix_rag_audit_runs_partition_id", "rag_audit_runs", ["partition_id"])
    if not index_exists("rag_audit_runs", "ix_rag_audit_runs_partition_name"):
        op.create_index("ix_rag_audit_runs_partition_name", "rag_audit_runs", ["partition_name"])
    if not index_exists("rag_audit_runs", "ix_rag_audit_runs_status"):
        op.create_index("ix_rag_audit_runs_status", "rag_audit_runs", ["status"])
    if not index_exists("rag_audit_runs", "ix_rag_audit_runs_started_at"):
        op.create_index("ix_rag_audit_runs_started_at", "rag_audit_runs", ["started_at"])
    if not index_exists("rag_audit_runs", "ix_rag_audit_runs_partition_started"):
        op.create_index("ix_rag_audit_runs_partition_started", "rag_audit_runs", ["partition_name", "started_at"])
    if not index_exists("rag_audit_runs", "ix_rag_audit_runs_partition_status"):
        op.create_index("ix_rag_audit_runs_partition_status", "rag_audit_runs", ["partition_name", "status"])


def downgrade() -> None:
    """Downgrade schema."""
    if table_exists("rag_audit_runs"):
        op.drop_table("rag_audit_runs")
