"""add files.created_at

Revision ID: b7c1d2e3f4a5
Revises: 06dd2101ea3a
Create Date: 2026-06-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists

# revision identifiers, used by Alembic.
revision: str = "b7c1d2e3f4a5"
down_revision: str | Sequence[str] | None = "06dd2101ea3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the indexation timestamp to ``files``.

    Idempotent: ``Base.metadata.create_all()`` at app startup may have already
    added the column from the SQLAlchemy model on existing deployments.

    Existing rows have no recorded index time, so the ``server_default`` backfills
    them to the migration run time; newly indexed files get their true insert time.
    """
    if not column_exists("files", "created_at"):
        op.add_column(
            "files",
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    """Downgrade schema."""
    if column_exists("files", "created_at"):
        op.drop_column("files", "created_at")
