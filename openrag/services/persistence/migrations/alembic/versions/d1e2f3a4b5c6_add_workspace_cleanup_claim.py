"""Serialize workspace orphan cleanup with file attachment.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
"""

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import column_exists, table_exists

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("files") and not column_exists("files", "workspace_cleanup_claimed"):
        op.add_column(
            "files",
            sa.Column("workspace_cleanup_claimed", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if column_exists("files", "workspace_cleanup_claimed"):
        op.drop_column("files", "workspace_cleanup_claimed")
