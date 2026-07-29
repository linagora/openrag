"""add user display name prefix index

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_users_lower_display_name_pattern"


def _index_validity() -> bool | None:
    result = op.get_bind().execute(
        sa.text(
            """
            SELECT i.indisvalid
            FROM pg_catalog.pg_class index_class
            JOIN pg_catalog.pg_index i ON i.indexrelid = index_class.oid
            JOIN pg_catalog.pg_class table_class ON table_class.oid = i.indrelid
            JOIN pg_catalog.pg_namespace namespace ON namespace.oid = index_class.relnamespace
            WHERE namespace.nspname = current_schema()
              AND table_class.relname = 'users'
              AND index_class.relname = :index_name
            """,
        ),
        {"index_name": _INDEX_NAME},
    )
    validity = result.scalar()
    return bool(validity) if validity is not None else None


def _drop_index_concurrently() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY {_INDEX_NAME}"))


def upgrade() -> None:
    if not table_exists("users"):
        return

    validity = _index_validity()
    if validity is False:
        _drop_index_concurrently()

    if validity is not True:
        with op.get_context().autocommit_block():
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY {_INDEX_NAME} ON users (LOWER(display_name) text_pattern_ops)",
                ),
            )


def downgrade() -> None:
    if table_exists("users") and _index_validity() is not None:
        _drop_index_concurrently()
