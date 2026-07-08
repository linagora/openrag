"""merge heads

Revision ID: cd9b84278028
Revises: a1b2c3d4e5f6, c224d4befe71
Create Date: 2026-02-13 09:31:59.531368

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "cd9b84278028"
down_revision: str | Sequence[str] | None = ("a1b2c3d4e5f6", "c224d4befe71")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
