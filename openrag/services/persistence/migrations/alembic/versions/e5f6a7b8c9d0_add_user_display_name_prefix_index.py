"""add user display name prefix index

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import index_exists, table_exists

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_users_lower_display_name_pattern"


def upgrade() -> None:
    if table_exists("users") and not index_exists("users", _INDEX_NAME):
        op.execute(
            sa.text(
                f"CREATE INDEX {_INDEX_NAME} ON users (LOWER(display_name) text_pattern_ops)",
            ),
        )


def downgrade() -> None:
    if table_exists("users") and index_exists("users", _INDEX_NAME):
        op.drop_index(_INDEX_NAME, table_name="users")
