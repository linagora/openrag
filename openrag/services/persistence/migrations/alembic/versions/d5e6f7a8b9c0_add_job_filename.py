"""add filename to durable indexing jobs

Revision ID: d5e6f7a8b9c0
Revises: c4e8f2a6b913
Create Date: 2026-09-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists

revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c4e8f2a6b913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if table_exists("jobs") and not column_exists("jobs", "filename"):
        op.add_column("jobs", sa.Column("filename", sa.String(), nullable=True))


def downgrade() -> None:
    if table_exists("jobs") and column_exists("jobs", "filename"):
        op.drop_column("jobs", "filename")
