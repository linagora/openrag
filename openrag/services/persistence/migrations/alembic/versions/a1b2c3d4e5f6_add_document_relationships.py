"""add document relationships

Revision ID: a1b2c3d4e5f6
Revises: cd642e4502d8
Create Date: 2026-01-05 14:00:00.000000

This migration adds relationship_id and parent_id columns to the files table
to support document relationships (e.g., email threads, folder hierarchies).

- relationship_id: Groups related documents together (e.g., email thread ID, folder path)
- parent_id: Hierarchical parent reference (e.g., parent email file_id, parent folder)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, index_exists

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "cd642e4502d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add relationship_id and parent_id columns to files table."""

    # Add relationship_id column
    if not column_exists("files", "relationship_id"):
        op.add_column(
            "files",
            sa.Column("relationship_id", sa.String(), nullable=True),
        )

    # Add parent_id column
    if not column_exists("files", "parent_id"):
        op.add_column(
            "files",
            sa.Column("parent_id", sa.String(), nullable=True),
        )

    # Create single-column indexes
    if not index_exists("files", "ix_files_relationship_id"):
        op.create_index(
            "ix_files_relationship_id",
            "files",
            ["relationship_id"],
            unique=False,
        )

    if not index_exists("files", "ix_files_parent_id"):
        op.create_index(
            "ix_files_parent_id",
            "files",
            ["parent_id"],
            unique=False,
        )

    # Create composite indexes for common query patterns
    if not index_exists("files", "ix_relationship_partition"):
        op.create_index(
            "ix_relationship_partition",
            "files",
            ["relationship_id", "partition_name"],
            unique=False,
        )

    if not index_exists("files", "ix_parent_partition"):
        op.create_index(
            "ix_parent_partition",
            "files",
            ["parent_id", "partition_name"],
            unique=False,
        )


def downgrade() -> None:
    """Remove relationship_id and parent_id columns from files table."""

    # Drop composite indexes
    if index_exists("files", "ix_parent_partition"):
        op.drop_index("ix_parent_partition", table_name="files")

    if index_exists("files", "ix_relationship_partition"):
        op.drop_index("ix_relationship_partition", table_name="files")

    # Drop single-column indexes
    if index_exists("files", "ix_files_parent_id"):
        op.drop_index("ix_files_parent_id", table_name="files")

    if index_exists("files", "ix_files_relationship_id"):
        op.drop_index("ix_files_relationship_id", table_name="files")

    # Drop columns
    if column_exists("files", "parent_id"):
        op.drop_column("files", "parent_id")

    if column_exists("files", "relationship_id"):
        op.drop_column("files", "relationship_id")
