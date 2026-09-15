"""merge heads

Revision ID: b8e2c4f60a17
Revises: 1f53920217de, e1f3a5c7d9b2
Create Date: 2026-09-15 00:00:00.000000

The per-embedder vector field work (#762) branched off before develop's jobs
and workspace-cleanup revisions, so merging develop back in left two heads and
`upgrade head` had nothing to resolve. Empty on purpose: the two branches touch
different tables.

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b8e2c4f60a17"
down_revision: str | Sequence[str] | None = ("1f53920217de", "e1f3a5c7d9b2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
