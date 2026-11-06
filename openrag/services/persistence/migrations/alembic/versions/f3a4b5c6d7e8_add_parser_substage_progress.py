"""Persist the latest observed parser substage for job progress.

Revision ID: f3a4b5c6d7e8
Revises: f2a3b4c5d6e7
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists

revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not table_exists("job_stage_state"):
        return

    columns = (
        ("current_substage_id", sa.String(length=64)),
        ("current_substage_label", sa.String(length=160)),
        ("current_substage_progress", sa.BigInteger()),
        ("current_substage_total", sa.BigInteger()),
    )
    for name, column_type in columns:
        if not column_exists("job_stage_state", name):
            op.add_column("job_stage_state", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    if not table_exists("job_stage_state"):
        return

    for name in (
        "current_substage_total",
        "current_substage_progress",
        "current_substage_label",
        "current_substage_id",
    ):
        if column_exists("job_stage_state", name):
            op.drop_column("job_stage_state", name)
