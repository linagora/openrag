"""index files by source

Before an uploaded original is deleted, the catalog is asked whether another
row still references it: a copy keeps its source's ``source``. The index
makes that lookup, by ``file_metadata ->> 'source'``, an index scan.

Revision ID: 9ed228e7ea17
Revises: 934e9f4e871f
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op
from schema_helpers import index_exists

revision: str = "9ed228e7ea17"
down_revision: str | Sequence[str] | None = "934e9f4e871f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "files"
_INDEX = "ix_files_source"


def upgrade() -> None:
    # Guarded: ``metadata.create_all()`` runs at startup and may have created
    # the index before alembic reaches this revision.
    if index_exists(_TABLE, _INDEX):
        return
    op.execute(f"CREATE INDEX {_INDEX} ON {_TABLE} ((file_metadata ->> 'source'))")


def downgrade() -> None:
    if not index_exists(_TABLE, _INDEX):
        return
    op.drop_index(_INDEX, table_name=_TABLE)
