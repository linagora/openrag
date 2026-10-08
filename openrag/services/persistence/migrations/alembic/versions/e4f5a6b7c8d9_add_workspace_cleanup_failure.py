"""Persist failed workspace cleanup for safe retries.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
"""

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import column_exists, table_exists

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("files") and not column_exists("files", "workspace_cleanup_failed"):
        op.add_column(
            "files",
            sa.Column("workspace_cleanup_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if column_exists("files", "workspace_cleanup_failed"):
        op.drop_column("files", "workspace_cleanup_failed")
