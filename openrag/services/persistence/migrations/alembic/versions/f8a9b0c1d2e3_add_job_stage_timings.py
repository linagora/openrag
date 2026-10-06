"""add per-stage timings to indexing jobs

Revision ID: f8a9b0c1d2e3
Revises: c4e8f2a6b913
Create Date: 2026-10-05 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists
from sqlalchemy.dialects import postgresql

revision: str = "f8a9b0c1d2e3"
down_revision: str | Sequence[str] | None = "c4e8f2a6b913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if table_exists("jobs") and not column_exists("jobs", "stage_timings"):
        op.add_column("jobs", sa.Column("stage_timings", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    if table_exists("jobs") and column_exists("jobs", "stage_timings"):
        op.drop_column("jobs", "stage_timings")
