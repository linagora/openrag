"""merge indexing stage timing and managed-user migration heads

Revision ID: 6f7a8b9c0d1e
Revises: f34e90d25e47, 5e6d7c8b9a01
Create Date: 2026-10-19

"""

from collections.abc import Sequence

revision: str = "6f7a8b9c0d1e"
down_revision: str | Sequence[str] | None = ("f34e90d25e47", "5e6d7c8b9a01")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
