"""Add queue fields to indexing jobs and files.

Revision ID: d9f1c27b5e40
Revises: f4a5b6c7d8e9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, index_exists
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import TextClause

revision: str = "d9f1c27b5e40"
down_revision: str | Sequence[str] | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "jobs"
_CLAIM_INDEX = "ix_jobs_queue_claim"
_RUNNING_INDEX = "ix_jobs_queue_running_user"


def _queue_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("lane", sa.Text()),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("object_key", sa.Text()),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("superseded_by", sa.Text()),
    )


def _index_validity(name: str) -> bool | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT i.indisvalid
            FROM pg_catalog.pg_index AS i
            JOIN pg_catalog.pg_class AS idx ON idx.oid = i.indexrelid
            WHERE i.indrelid = to_regclass(:table_name)
              AND idx.relname = :index_name
            """
            ),
            {"table_name": _TABLE, "index_name": name},
        )
        .scalar_one_or_none()
    )


def _create_queue_index(name: str, columns: list[str | TextClause], predicate: str) -> None:
    validity = _index_validity(name)
    if validity is True:
        return

    # An interrupted concurrent build leaves an invalid catalog entry. Remove
    # it before retrying. Entering autocommit releases the column DDL locks.
    with op.get_context().autocommit_block():
        if validity is False:
            op.drop_index(name, table_name=_TABLE, postgresql_concurrently=True)
        op.create_index(
            name,
            _TABLE,
            columns,
            postgresql_where=sa.text(predicate),
            postgresql_concurrently=True,
        )


def upgrade() -> None:
    if not column_exists("files", "indexed_by_job"):
        op.add_column("files", sa.Column("indexed_by_job", sa.Text(), nullable=True))

    for column in _queue_columns():
        if not column_exists(_TABLE, column.name):
            op.add_column(_TABLE, column)

    # Keep room for heartbeat updates on the same page. Vacuum before dead
    # tuples from these updates accumulate against a large terminal history.
    op.execute("ALTER TABLE jobs SET (fillfactor = 80, autovacuum_vacuum_scale_factor = 0.01)")

    _create_queue_index(_CLAIM_INDEX, ["lane", sa.text("priority DESC"), "available_at"], "status = 'QUEUED'")
    _create_queue_index(_RUNNING_INDEX, ["user_id"], "status = 'SERIALIZING' AND lane IS NOT NULL")


def downgrade() -> None:
    if column_exists("files", "indexed_by_job"):
        op.drop_column("files", "indexed_by_job")

    op.execute("ALTER TABLE jobs RESET (fillfactor, autovacuum_vacuum_scale_factor)")
    for index in (_RUNNING_INDEX, _CLAIM_INDEX):
        if index_exists(_TABLE, index):
            op.drop_index(index, table_name=_TABLE)

    for column in reversed(_queue_columns()):
        if column_exists(_TABLE, column.name):
            op.drop_column(_TABLE, column.name)
