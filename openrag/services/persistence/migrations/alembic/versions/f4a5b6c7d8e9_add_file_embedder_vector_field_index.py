"""index file embedder vector-field references

Revision ID: f4a5b6c7d8e9
Revises: a2b3c4d5e6f7
Create Date: 2026-11-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import table_exists

revision: str = "f4a5b6c7d8e9"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_files_embedder_vector_field"


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
              AND table_class.relname = 'files'
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
    if not table_exists("files"):
        return

    validity = _index_validity()
    if validity is False:
        _drop_index_concurrently()

    if validity is not True:
        with op.get_context().autocommit_block():
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY {_INDEX_NAME} ON files "
                    "((indexation_config->>'embedder_vector_field')) "
                    "WHERE indexation_config->>'embedder_vector_field' IS NOT NULL",
                ),
            )


def downgrade() -> None:
    if table_exists("files") and _index_validity() is not None:
        _drop_index_concurrently()
