"""index files by source

Before an uploaded original is deleted, the catalog is asked whether another
row still references it: a copy keeps its source's ``source``. The index
makes that lookup, by ``file_metadata ->> 'source'``, an index scan.

It is built ``CONCURRENTLY``, so writes to ``files`` go on while it builds.
A build that failed leaves an invalid index behind, which the next upgrade
drops and builds again.

Revision ID: 9ed228e7ea17
Revises: a2b3c4d5e6f7
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists

revision: str = "9ed228e7ea17"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "files"
_INDEX = "ix_files_source"


def _index_validity() -> bool | None:
    """``True`` for a usable index, ``False`` for one a failed build left, ``None`` for none."""
    result = op.get_bind().execute(
        sa.text(
            """
            SELECT i.indisvalid
            FROM pg_catalog.pg_class index_class
            JOIN pg_catalog.pg_index i ON i.indexrelid = index_class.oid
            JOIN pg_catalog.pg_class table_class ON table_class.oid = i.indrelid
            JOIN pg_catalog.pg_namespace namespace ON namespace.oid = index_class.relnamespace
            WHERE namespace.nspname = current_schema()
              AND table_class.relname = :table_name
              AND index_class.relname = :index_name
            """,
        ),
        {"table_name": _TABLE, "index_name": _INDEX},
    )
    validity = result.scalar()
    return bool(validity) if validity is not None else None


def _drop_index_concurrently() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY {_INDEX}"))


def upgrade() -> None:
    if not table_exists(_TABLE):
        return
    # ``metadata.create_all()`` runs at startup and may have built it already.
    validity = _index_validity()
    if validity is True:
        return
    if validity is False:
        _drop_index_concurrently()
    # Outside alembic's transaction, which ``CONCURRENTLY`` refuses.
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"CREATE INDEX CONCURRENTLY {_INDEX} ON {_TABLE} ((file_metadata ->> 'source'))"))


def downgrade() -> None:
    if table_exists(_TABLE) and _index_validity() is not None:
        _drop_index_concurrently()
