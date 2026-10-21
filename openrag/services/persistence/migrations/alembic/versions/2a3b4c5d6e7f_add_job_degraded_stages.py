"""add job degraded stages

Revision ID: 2a3b4c5d6e7f
Revises: 09f6c4b8a2d1
Create Date: 2026-09-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists
from sqlalchemy.dialects import postgresql

revision: str = "2a3b4c5d6e7f"
down_revision: str | Sequence[str] | None = "09f6c4b8a2d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if table_exists("jobs") and not column_exists("jobs", "degraded_stages"):
        op.add_column(
            "jobs",
            sa.Column(
                "degraded_stages",
                postgresql.ARRAY(sa.String()),
                nullable=False,
                server_default=sa.text("ARRAY[]::text[]"),
            ),
        )


def downgrade() -> None:
    if table_exists("jobs") and column_exists("jobs", "degraded_stages"):
        op.drop_column("jobs", "degraded_stages")
