"""add jobs table

Revision ID: c7d8e9f0a1b2
Revises: b9c0d1e2f3a4
Create Date: 2026-09-07 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import index_exists, table_exists

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not table_exists("jobs"):
        op.create_table(
            "jobs",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("partition", sa.String(), nullable=False),
            sa.Column("file_id", sa.String(), nullable=True),
            sa.Column("user_id", sa.BigInteger(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not index_exists("jobs", "ix_jobs_status"):
        op.create_index("ix_jobs_status", "jobs", ["status"])
    if not index_exists("jobs", "ix_jobs_user_id"):
        op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    if not index_exists("jobs", "ix_jobs_partition_file_id"):
        op.create_index("ix_jobs_partition_file_id", "jobs", ["partition", "file_id"])


def downgrade() -> None:
    if index_exists("jobs", "ix_jobs_partition_file_id"):
        op.drop_index("ix_jobs_partition_file_id", table_name="jobs")
    if index_exists("jobs", "ix_jobs_user_id"):
        op.drop_index("ix_jobs_user_id", table_name="jobs")
    if index_exists("jobs", "ix_jobs_status"):
        op.drop_index("ix_jobs_status", table_name="jobs")
    if table_exists("jobs"):
        op.drop_table("jobs")
