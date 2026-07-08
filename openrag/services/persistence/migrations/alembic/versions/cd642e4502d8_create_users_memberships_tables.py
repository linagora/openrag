"""create users memberships tables

Revision ID: cd642e4502d8
Revises: 4add4d260575
Create Date: 2025-10-27 15:00:40.022871

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import index_exists, table_exists

# revision identifiers, used by Alembic.
revision: str = "cd642e4502d8"
down_revision: str | Sequence[str] | None = "4add4d260575"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create users table if it doesn't exist
    if not table_exists("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("external_user_id", sa.String(), nullable=True),
            sa.Column("display_name", sa.String(), nullable=True),
            sa.Column("token", sa.String(), nullable=True),
            sa.Column("is_admin", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    # Create indexes for users table if they don't exist
    if table_exists("users"):
        if not index_exists("users", "ix_users_external_user_id"):
            op.create_index(
                op.f("ix_users_external_user_id"),
                "users",
                ["external_user_id"],
                unique=True,
            )
        if not index_exists("users", "ix_users_token"):
            op.create_index(op.f("ix_users_token"), "users", ["token"], unique=True)

    # Create partition_memberships table if it doesn't exist
    if not table_exists("partition_memberships"):
        op.create_table(
            "partition_memberships",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("partition_name", sa.String(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("added_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("role IN ('owner','editor','viewer')", name="ck_membership_role"),
            sa.ForeignKeyConstraint(["partition_name"], ["partitions.partition"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("partition_name", "user_id", name="uix_partition_user"),
        )

    # Create indexes for partition_memberships table if they don't exist
    if table_exists("partition_memberships"):
        if not index_exists("partition_memberships", "ix_partition_memberships_partition_name"):
            op.create_index(
                op.f("ix_partition_memberships_partition_name"),
                "partition_memberships",
                ["partition_name"],
                unique=False,
            )
        if not index_exists("partition_memberships", "ix_partition_memberships_user_id"):
            op.create_index(
                op.f("ix_partition_memberships_user_id"),
                "partition_memberships",
                ["user_id"],
                unique=False,
            )
        if not index_exists("partition_memberships", "ix_user_partition"):
            op.create_index(
                "ix_user_partition",
                "partition_memberships",
                ["user_id", "partition_name"],
                unique=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop indexes and tables if they exist
    if table_exists("partition_memberships"):
        if index_exists("partition_memberships", "ix_user_partition"):
            op.drop_index("ix_user_partition", table_name="partition_memberships")
        if index_exists("partition_memberships", "ix_partition_memberships_user_id"):
            op.drop_index(
                op.f("ix_partition_memberships_user_id"),
                table_name="partition_memberships",
            )
        if index_exists("partition_memberships", "ix_partition_memberships_partition_name"):
            op.drop_index(
                op.f("ix_partition_memberships_partition_name"),
                table_name="partition_memberships",
            )
        op.drop_table("partition_memberships")

    if table_exists("users"):
        if index_exists("users", "ix_users_token"):
            op.drop_index(op.f("ix_users_token"), table_name="users")
        if index_exists("users", "ix_users_external_user_id"):
            op.drop_index(op.f("ix_users_external_user_id"), table_name="users")
        op.drop_table("users")
