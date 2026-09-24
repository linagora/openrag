"""add job error reason

Revision ID: 3b4c5d6e7f8a
Revises: 2a3b4c5d6e7f
Create Date: 2026-09-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists

revision: str = "3b4c5d6e7f8a"
down_revision: str | Sequence[str] | None = "2a3b4c5d6e7f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if table_exists("jobs") and not column_exists("jobs", "error_reason"):
        op.add_column("jobs", sa.Column("error_reason", sa.String(), nullable=True))


def downgrade() -> None:
    if table_exists("jobs") and column_exists("jobs", "error_reason"):
        op.drop_column("jobs", "error_reason")
