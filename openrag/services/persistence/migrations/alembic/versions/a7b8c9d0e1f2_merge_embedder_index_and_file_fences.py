"""merge embedder index and file fence heads

Revision ID: a7b8c9d0e1f2
Revises: f4a5b6c7d8e9, 934e9f4e871f
Create Date: 2026-11-20 12:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = ("f4a5b6c7d8e9", "934e9f4e871f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
