"""add evaluation datasets and runs

Revision ID: a7c9e1f2b3d4
Revises: d4e5f6a7b8c9
Create Date: 2026-07-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7c9e1f2b3d4"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Idempotent: ``Base.metadata.create_all()`` runs at startup, so a freshly
    bootstrapped database already has these tables before alembic sees them.
    """
    if not table_exists("eval_datasets"):
        op.create_table(
            "eval_datasets",
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("name", sa.String, nullable=False),
            sa.Column("corpus_file_count", sa.Integer, server_default="0", nullable=False),
            sa.Column("testset_row_count", sa.Integer, server_default="0", nullable=False),
            sa.Column(
                "created_by",
                sa.Integer,
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
    if not table_exists("eval_runs"):
        op.create_table(
            "eval_runs",
            sa.Column("id", sa.String, primary_key=True),
            sa.Column(
                "dataset_id",
                sa.String,
                sa.ForeignKey("eval_datasets.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("status", sa.String, server_default="QUEUED", nullable=False),
            sa.Column("indexing", postgresql.JSONB, nullable=True),
            sa.Column("retrieval", postgresql.JSONB, nullable=True),
            sa.Column("answer", postgresql.JSONB, nullable=True),
            sa.Column("cases", postgresql.JSONB, nullable=True),
            sa.Column("error", sa.String, nullable=True),
            sa.Column(
                "created_by",
                sa.Integer,
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
                index=True,
            ),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "status IN ('QUEUED','INDEXING','EVALUATING','COMPLETED','FAILED','CANCELLED')",
                name="ck_eval_run_status",
            ),
        )


def downgrade() -> None:
    """Downgrade schema."""
    if table_exists("eval_runs"):
        op.drop_table("eval_runs")
    if table_exists("eval_datasets"):
        op.drop_table("eval_datasets")
