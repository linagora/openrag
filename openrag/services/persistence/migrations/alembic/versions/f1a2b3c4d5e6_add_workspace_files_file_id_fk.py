"""add FK from workspace_files.file_id (int) to files.id

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-03-12 10:00:00.000000

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import (
    column_exists,
    column_type_is,
    fk_exists,
    index_exists,
    table_exists,
    unique_constraint_exists,
)

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

#: Rows this migration removes are copied here instead of being dropped outright.
#: A follow-up migration can drop the table once operators have reviewed it.
ORPHAN_TABLE = "workspace_files_orphans_f1a2b3c4d5e6"


def _quarantine_delete(condition: str, reason: str) -> None:
    """Delete the workspace_files rows matching `condition`, keeping a copy.

    The delete and the copy are a single statement, so they share the
    migration's transaction: either both happen or neither does.
    """
    result = op.get_bind().execute(
        sa.text(
            f"WITH removed AS ("
            f"  DELETE FROM workspace_files wf WHERE {condition}"
            f"  RETURNING wf.workspace_id, wf.file_id"
            f") INSERT INTO {ORPHAN_TABLE} (workspace_id, file_id, reason) "
            f"SELECT workspace_id, file_id, :reason FROM removed"
        ),
        {"reason": reason},
    )
    logger.info("workspace_files: quarantined %s row(s) in %s (%s)", result.rowcount, ORPHAN_TABLE, reason)


def upgrade() -> None:
    """Migrate workspace_files.file_id from string to integer FK referencing files.id.

    Idempotent: older deployments may already have workspace_files with
    file_id as INTEGER, in which case the conversion is a no-op.
    """
    if column_type_is("workspace_files", "file_id", sa.Integer):
        return

    null_file_id_count = (
        op.get_bind().execute(sa.text("SELECT COUNT(*) FROM workspace_files WHERE file_id IS NULL")).scalar_one()
    )
    if null_file_id_count:
        raise RuntimeError(f"Cannot migrate workspace_files: {null_file_id_count} row(s) have NULL file_id")

    if not table_exists(ORPHAN_TABLE):
        op.create_table(
            ORPHAN_TABLE,
            sa.Column("workspace_id", sa.String, nullable=False),
            sa.Column("file_id", sa.String, nullable=False),
            sa.Column("reason", sa.String, nullable=False),
        )

    # 1. Purge rows that have no matching file (no valid files.file_id to JOIN against).
    #    NOT EXISTS rather than NOT IN: a NULL in the subquery would make NOT IN
    #    match nothing and turn the purge into a silent no-op.
    _quarantine_delete("NOT EXISTS (SELECT 1 FROM files f WHERE f.file_id = wf.file_id)", "no_matching_file")

    # 2. Add a temporary integer column to hold the resolved files.id value.
    if not column_exists("workspace_files", "file_fk"):
        op.add_column("workspace_files", sa.Column("file_fk", sa.Integer(), nullable=True))

    # 3. Populate it by joining on the string file_id, scoped to the workspace's partition
    #    to resolve ambiguity when the same filename exists in multiple partitions.
    #    All joined tables go in the FROM list — Postgres doesn't allow forward
    #    references to the UPDATE target (wf) inside a from_item's JOIN ON clause.
    op.execute(
        "UPDATE workspace_files wf "
        "SET file_fk = f.id "
        "FROM files f, workspaces w "
        "WHERE w.workspace_id = wf.workspace_id "
        "  AND f.file_id = wf.file_id "
        "  AND f.partition_name = w.partition_name"
    )

    # 3b. Drop any rows that couldn't be resolved (file_fk still NULL).
    _quarantine_delete("wf.file_fk IS NULL", "unresolved_partition")

    # 4. Drop the old string column and its index.
    if index_exists("workspace_files", "ix_workspace_files_file_id"):
        op.drop_index("ix_workspace_files_file_id", table_name="workspace_files")
    if column_exists("workspace_files", "file_id"):
        op.drop_column("workspace_files", "file_id")

    # 5. Rename file_fk → file_id, make it NOT NULL.
    op.alter_column("workspace_files", "file_fk", new_column_name="file_id", nullable=False)

    # 6. Recreate the index, unique constraint, and FK.
    if not index_exists("workspace_files", "ix_workspace_files_file_id"):
        op.create_index("ix_workspace_files_file_id", "workspace_files", ["file_id"])
    if not unique_constraint_exists("workspace_files", "uix_workspace_file"):
        op.create_unique_constraint("uix_workspace_file", "workspace_files", ["workspace_id", "file_id"])
    if not fk_exists("workspace_files", "fk_workspace_files_file_id"):
        op.create_foreign_key(
            "fk_workspace_files_file_id",
            "workspace_files",
            "files",
            ["file_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    """Revert workspace_files.file_id back to a string column."""
    if fk_exists("workspace_files", "fk_workspace_files_file_id"):
        op.drop_constraint("fk_workspace_files_file_id", "workspace_files", type_="foreignkey")
    if index_exists("workspace_files", "ix_workspace_files_file_id"):
        op.drop_index("ix_workspace_files_file_id", table_name="workspace_files")

    # Re-add a string column and repopulate from files.file_id via JOIN.
    if not column_exists("workspace_files", "file_str"):
        op.add_column("workspace_files", sa.Column("file_str", sa.String(), nullable=True))
    op.execute("UPDATE workspace_files wf SET file_str = f.file_id FROM files f WHERE f.id = wf.file_id")
    if column_exists("workspace_files", "file_id"):
        op.drop_column("workspace_files", "file_id")
    op.alter_column("workspace_files", "file_str", new_column_name="file_id", nullable=False)
    if not index_exists("workspace_files", "ix_workspace_files_file_id"):
        op.create_index("ix_workspace_files_file_id", "workspace_files", ["file_id"])
    if not unique_constraint_exists("workspace_files", "uix_workspace_file"):
        op.create_unique_constraint("uix_workspace_file", "workspace_files", ["workspace_id", "file_id"])

    # Put back the rows upgrade() quarantined, now that file_id is a string again.
    # Workspaces deleted since the upgrade are skipped: their FK no longer resolves.
    if table_exists(ORPHAN_TABLE):
        op.execute(
            f"INSERT INTO workspace_files (workspace_id, file_id) "
            f"SELECT o.workspace_id, o.file_id FROM {ORPHAN_TABLE} o "
            f"WHERE EXISTS (SELECT 1 FROM workspaces w WHERE w.workspace_id = o.workspace_id) "
            f"ON CONFLICT ON CONSTRAINT uix_workspace_file DO NOTHING"
        )
        op.execute(f"DROP TABLE {ORPHAN_TABLE}")
