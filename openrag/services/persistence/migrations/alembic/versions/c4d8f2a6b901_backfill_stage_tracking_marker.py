"""Backfill terminal jobs settled before stage tracking was wired in.

Revision ID: c4d8f2a6b901
Revises: bcbdf503a6f1
Create Date: 2026-10-29
"""

from collections.abc import Sequence

from alembic import op
from schema_helpers import column_exists, table_exists

revision: str = "c4d8f2a6b901"
down_revision: str | Sequence[str] | None = "bcbdf503a6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if table_exists("jobs") and column_exists("jobs", "stage_tracking_version"):
        op.execute(
            """
            UPDATE jobs
            SET stage_tracking_version = 0
            WHERE stage_tracking_version IS NULL
              AND status IN ('COMPLETED', 'FAILED', 'CANCELLED')
            """
        )


def downgrade() -> None:
    # The historical marker cannot be safely distinguished from later writes.
    pass
