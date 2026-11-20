"""Merge the file quota and embedder index migration heads.

Revision ID: f83c19a6d2e4
Revises: a2b3c4d5e6f7, c1d2e3f4a5b6
Create Date: 2026-11-20
"""

from collections.abc import Sequence

revision: str = "f83c19a6d2e4"
down_revision: str | Sequence[str] | None = ("a2b3c4d5e6f7", "c1d2e3f4a5b6")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without changing the schema."""


def downgrade() -> None:
    """Return to the two parent revisions."""
