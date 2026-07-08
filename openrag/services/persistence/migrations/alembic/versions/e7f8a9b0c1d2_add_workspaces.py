"""add workspaces

Revision ID: e7f8a9b0c1d2
Revises: cd9b84278028
Create Date: 2026-03-06 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: str | Sequence[str] | None = "cd9b84278028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Idempotent: older deployments may already contain these tables.
    """
    if not table_exists("workspaces"):
        op.create_table(
            "workspaces",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("workspace_id", sa.String, unique=True, nullable=False, index=True),
            sa.Column(
                "partition_name",
                sa.String,
                sa.ForeignKey("partitions.partition", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_by",
                sa.Integer,
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("display_name", sa.String, nullable=True),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        )
    if not table_exists("workspace_files"):
        op.create_table(
            "workspace_files",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String,
                sa.ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("file_id", sa.String, nullable=False, index=True),
            sa.UniqueConstraint("workspace_id", "file_id", name="uix_workspace_file"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    if table_exists("workspace_files"):
        op.drop_table("workspace_files")
    if table_exists("workspaces"):
        op.drop_table("workspaces")
