"""Prevent recovery after destructive workspace cleanup starts.

Revision ID: d3e4f5a6b7c8
Revises: d2e3f4a5b6c7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import column_exists, table_exists

revision: str = "d3e4f5a6b7c8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if table_exists("files") and not column_exists("files", "workspace_cleanup_started"):
        op.add_column(
            "files",
            sa.Column("workspace_cleanup_started", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if column_exists("files", "workspace_cleanup_started"):
        op.drop_column("files", "workspace_cleanup_started")
