"""add file_count and file_quota

Revision ID: c224d4befe71
Revises: cd642e4502d8
Create Date: 2026-02-09 15:38:28.565172

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, fk_exists, index_exists

# revision identifiers, used by Alembic.
revision: str = "c224d4befe71"
down_revision: str | Sequence[str] | None = "cd642e4502d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Idempotent: older deployments may already contain these columns/indexes.
    """
    if not column_exists("users", "file_quota"):
        op.add_column("users", sa.Column("file_quota", sa.Integer(), nullable=True))
    if not column_exists("users", "file_count"):
        op.add_column("users", sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"))
    if not column_exists("files", "created_by"):
        op.add_column("files", sa.Column("created_by", sa.Integer(), nullable=True))
    if not fk_exists("files", "fk_files_created_by"):
        op.create_foreign_key("fk_files_created_by", "files", "users", ["created_by"], ["id"], ondelete="SET NULL")
    if not index_exists("files", "ix_files_created_by"):
        op.create_index("ix_files_created_by", "files", ["created_by"])


def downgrade() -> None:
    """Downgrade schema."""
    if index_exists("files", "ix_files_created_by"):
        op.drop_index("ix_files_created_by", table_name="files")
    if fk_exists("files", "fk_files_created_by"):
        op.drop_constraint("fk_files_created_by", "files", type_="foreignkey")
    if column_exists("files", "created_by"):
        op.drop_column("files", "created_by")
    if column_exists("users", "file_count"):
        op.drop_column("users", "file_count")
    if column_exists("users", "file_quota"):
        op.drop_column("users", "file_quota")
