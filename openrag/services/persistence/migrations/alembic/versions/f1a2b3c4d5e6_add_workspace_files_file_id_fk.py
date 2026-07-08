"""add FK from workspace_files.file_id (int) to files.id

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-03-12 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import (
    column_exists,
    column_type_is,
    fk_exists,
    index_exists,
    unique_constraint_exists,
)

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Migrate workspace_files.file_id from string to integer FK referencing files.id.

    Idempotent: older deployments may already have workspace_files with
    file_id as INTEGER, in which case the conversion is a no-op.
    """
    if column_type_is("workspace_files", "file_id", sa.Integer):
        return

    # 1. Purge rows that have no matching file (no valid files.file_id to JOIN against).
    op.execute("DELETE FROM workspace_files WHERE file_id NOT IN (SELECT file_id FROM files)")

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
    op.execute("DELETE FROM workspace_files WHERE file_fk IS NULL")

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
