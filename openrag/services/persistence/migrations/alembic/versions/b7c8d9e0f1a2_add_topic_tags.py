"""add topic_tags table

Revision ID: b7c8d9e0f1a2
Revises: 06dd2101ea3a
Create Date: 2026-06-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import index_exists, table_exists, unique_constraint_exists

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "06dd2101ea3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not table_exists("topic_tags"):
        op.create_table(
            "topic_tags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("document_id", sa.String(), nullable=False),
            sa.Column("partition", sa.String(), nullable=False),
            sa.Column("tag", sa.String(), nullable=False),
            sa.Column("normalized_tag", sa.String(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["document_id", "partition"],
                ["files.file_id", "files.partition_name"],
                ondelete="CASCADE",
                name="fk_topic_tags_file",
            ),
            sa.UniqueConstraint(
                "document_id",
                "partition",
                "normalized_tag",
                name="uix_topic_tags_document_partition_tag",
            ),
        )

    if not index_exists("topic_tags", "ix_topic_tags_partition"):
        op.create_index("ix_topic_tags_partition", "topic_tags", ["partition"])
    if not index_exists("topic_tags", "ix_topic_tags_document_id"):
        op.create_index("ix_topic_tags_document_id", "topic_tags", ["document_id"])
    if not index_exists("topic_tags", "ix_topic_tags_partition_tag"):
        op.create_index("ix_topic_tags_partition_tag", "topic_tags", ["partition", "normalized_tag"])
    if not unique_constraint_exists("topic_tags", "uix_topic_tags_document_partition_tag"):
        op.create_unique_constraint(
            "uix_topic_tags_document_partition_tag",
            "topic_tags",
            ["document_id", "partition", "normalized_tag"],
        )


def downgrade() -> None:
    if index_exists("topic_tags", "ix_topic_tags_partition_tag"):
        op.drop_index("ix_topic_tags_partition_tag", table_name="topic_tags")
    if index_exists("topic_tags", "ix_topic_tags_document_id"):
        op.drop_index("ix_topic_tags_document_id", table_name="topic_tags")
    if index_exists("topic_tags", "ix_topic_tags_partition"):
        op.drop_index("ix_topic_tags_partition", table_name="topic_tags")
    if table_exists("topic_tags"):
        op.drop_table("topic_tags")
