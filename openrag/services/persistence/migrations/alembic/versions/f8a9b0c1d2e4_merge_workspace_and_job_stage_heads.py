"""merge workspace and job stage timing migration heads

Revision ID: f8a9b0c1d2e4
Revises: a7b8c9d0e1f2, f8a9b0c1d2e3
Create Date: 2026-10-05

"""

from collections.abc import Sequence

revision: str = "f8a9b0c1d2e4"
down_revision: str | Sequence[str] | None = ("a7b8c9d0e1f2", "f8a9b0c1d2e3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
