"""add users.managed_by_config

Marks the accounts provisioned from ``auth.seed_users``. Startup seeding only
ever updates or revokes rows carrying the flag, so an account created
through the API or by an OIDC login is never taken over by a config entry
that happens to share its ``external_user_id``.

Revision ID: 5e6d7c8b9a01
Revises: 0794ddd13291
Create Date: 2026-10-09

"""

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import column_exists

revision = "5e6d7c8b9a01"
down_revision = "0794ddd13291"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not column_exists("users", "managed_by_config"):
        op.add_column(
            "users",
            sa.Column("managed_by_config", sa.Boolean(), server_default=sa.false(), nullable=False),
        )


def downgrade() -> None:
    if column_exists("users", "managed_by_config"):
        op.drop_column("users", "managed_by_config")
