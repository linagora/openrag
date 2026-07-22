"""add content claim ownership tokens

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not table_exists("file_content_claims") or column_exists("file_content_claims", "claim_token"):
        return

    op.add_column(
        "file_content_claims",
        sa.Column(
            "claim_token",
            sa.String(),
            server_default=sa.text("md5(random()::text || clock_timestamp()::text)"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    if table_exists("file_content_claims") and column_exists("file_content_claims", "claim_token"):
        op.drop_column("file_content_claims", "claim_token")
