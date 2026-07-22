"""add document content hashes

Revision ID: c3d4e5f6a7b8
Revises: b7c1d2e3f4a5
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, index_exists, table_exists

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b7c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not column_exists("files", "content_sha256"):
        op.add_column("files", sa.Column("content_sha256", sa.String(length=64), nullable=True))

    if not index_exists("files", "uix_files_partition_content_sha256"):
        op.create_index(
            "uix_files_partition_content_sha256",
            "files",
            ["partition_name", "content_sha256"],
            unique=True,
            postgresql_where=sa.text("content_sha256 IS NOT NULL"),
        )

    if not table_exists("file_content_claims"):
        op.create_table(
            "file_content_claims",
            sa.Column("partition_name", sa.String(), nullable=False),
            sa.Column("content_sha256", sa.String(length=64), nullable=False),
            sa.Column("file_id", sa.String(), nullable=False),
            sa.Column(
                "expires_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now() + interval '24 hours'"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["partition_name"],
                ["partitions.partition"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("partition_name", "content_sha256"),
        )


def downgrade() -> None:
    if table_exists("file_content_claims"):
        op.drop_table("file_content_claims")
    if index_exists("files", "uix_files_partition_content_sha256"):
        op.drop_index("uix_files_partition_content_sha256", table_name="files")
    if column_exists("files", "content_sha256"):
        op.drop_column("files", "content_sha256")
