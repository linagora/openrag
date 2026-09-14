"""Preserve files whose workspace ownership is unknown.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
"""

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import column_exists, table_exists

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("files") and not column_exists("files", "independently_indexed"):
        op.add_column(
            "files",
            sa.Column("independently_indexed", sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    if column_exists("files", "independently_indexed"):
        op.drop_column("files", "independently_indexed")
