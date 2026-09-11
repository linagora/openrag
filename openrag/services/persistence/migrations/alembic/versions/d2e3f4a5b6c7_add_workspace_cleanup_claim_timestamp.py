"""Allow abandoned workspace cleanup claims to be recovered.

Revision ID: d2e3f4a5b6c7
Revises: d1e2f3a4b5c6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import column_exists, table_exists

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if table_exists("files") and not column_exists("files", "workspace_cleanup_claimed_at"):
        op.add_column("files", sa.Column("workspace_cleanup_claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if column_exists("files", "workspace_cleanup_claimed_at"):
        op.drop_column("files", "workspace_cleanup_claimed_at")
