"""merge heads

Revision ID: 1f53920217de
Revises: c7d8e9f0a1b2, f5a6b7c8d9e0
Create Date: 2026-09-14 00:00:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "1f53920217de"
down_revision: str | Sequence[str] | None = ("c7d8e9f0a1b2", "f5a6b7c8d9e0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
