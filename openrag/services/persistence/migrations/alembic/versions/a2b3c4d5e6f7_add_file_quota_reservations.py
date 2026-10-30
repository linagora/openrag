"""add file_quota_reservations

One row per file slot a quota-capped upload holds before its file exists
(#664), so admission can reserve the slot atomically and every path that ends
without a file can give it back exactly once.

No data migration: ``users.file_count`` already counts each user's files, and
with no reservation rows yet that is exactly what it must hold.

Revision ID: a2b3c4d5e6f7
Revises: bcbdf503a6f1
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import index_exists, table_exists

revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "bcbdf503a6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "file_quota_reservations"
_USER_INDEX = "ix_file_quota_reservations_user_id"


def upgrade() -> None:
    # Guarded: ``metadata.create_all()`` runs at startup and may have created
    # the table before alembic reaches this revision.
    if not table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            # Unnamed, so this and ``create_all`` converge on the Postgres
            # default name.
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
    if not index_exists(_TABLE, _USER_INDEX):
        op.create_index(_USER_INDEX, _TABLE, ["user_id"])


def downgrade() -> None:
    # Outstanding reservations are still counted in ``users.file_count``.
    # Dropping the rows would leave those slots taken for good, so give them
    # back first.
    if table_exists(_TABLE):
        op.execute(
            f"""
            UPDATE users
               SET file_count = GREATEST(users.file_count - held.n, 0)
              FROM (SELECT user_id, COUNT(*)::int AS n FROM {_TABLE} GROUP BY user_id) AS held
             WHERE users.id = held.user_id
            """
        )
        if index_exists(_TABLE, _USER_INDEX):
            op.drop_index(_USER_INDEX, table_name=_TABLE)
        op.drop_table(_TABLE)
