"""Record worker settlement for interrupted stage history.

Revision ID: f2a3b4c5d6e7
Revises: c4d8f2a6b901
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists

revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "c4d8f2a6b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if table_exists("jobs") and not column_exists("jobs", "stage_interruption_confirmed_attempt"):
        op.add_column("jobs", sa.Column("stage_interruption_confirmed_attempt", sa.Integer(), nullable=True))


def downgrade() -> None:
    if table_exists("jobs") and column_exists("jobs", "stage_interruption_confirmed_attempt"):
        op.drop_column("jobs", "stage_interruption_confirmed_attempt")
