"""add users.email and oidc_sessions table for OIDC auth

Revision ID: f5b6c918f741
Revises: f1a2b3c4d5e6
Create Date: 2026-04-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5b6c918f741"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema: add users.email column and create oidc_sessions table."""
    # users.email (nullable, unique, indexed)
    op.add_column("users", sa.Column("email", sa.String(), nullable=True))
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # oidc_sessions table
    op.create_table(
        "oidc_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_token_hash",
            sa.String(length=64),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("sid", sa.String(), nullable=True, index=True),
        sa.Column("sub", sa.String(), nullable=False),
        sa.Column("id_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(), nullable=False),
        sa.Column("session_expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_refresh_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    # Composite index (user_id, sub) for fast lookup by (user, OIDC subject).
    # The individual session_token_hash, user_id, and sid indexes are already
    # created implicitly via the Column(..., index=True/unique=True) directives above.
    op.create_index(
        "ix_oidc_sessions_user_sub",
        "oidc_sessions",
        ["user_id", "sub"],
    )


def downgrade() -> None:
    """Downgrade schema: drop oidc_sessions table and users.email column."""
    op.drop_index("ix_oidc_sessions_user_sub", table_name="oidc_sessions")
    # Drop table — this cascades the implicit per-column indexes.
    op.drop_table("oidc_sessions")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "email")
