"""merge indexing stage timing and embedder swap migration heads

Revision ID: f34e90d25e47
Revises: f8a9b0c1d2e4, 0794ddd13291
Create Date: 2026-10-13

"""

from collections.abc import Sequence

revision: str = "f34e90d25e47"
down_revision: str | Sequence[str] | None = ("f8a9b0c1d2e4", "0794ddd13291")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
