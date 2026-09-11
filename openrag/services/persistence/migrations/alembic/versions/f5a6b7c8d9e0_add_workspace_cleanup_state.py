"""Add an explicit workspace cleanup state machine.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
"""

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import check_constraint_exists, column_exists, table_exists

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not table_exists("files"):
        return
    if not column_exists("files", "workspace_cleanup_state"):
        op.add_column(
            "files",
            sa.Column("workspace_cleanup_state", sa.String(), nullable=False, server_default="NONE"),
        )
        op.execute(
            """
        UPDATE files
        SET workspace_cleanup_state = CASE
            WHEN workspace_cleanup_failed THEN 'CLEANUP_FAILED'
            WHEN workspace_cleanup_started THEN 'CLEANUP_STARTED'
            WHEN workspace_cleanup_claimed THEN 'CLAIMED'
            ELSE 'NONE'
        END
        """
        )
    if not check_constraint_exists("files", "ck_files_workspace_cleanup_state"):
        op.create_check_constraint(
            "ck_files_workspace_cleanup_state",
            "files",
            "workspace_cleanup_state IN ('NONE', 'CLAIMED', 'CLEANUP_STARTED', 'CLEANUP_FAILED', 'CLEANUP_FINALIZED')",
        )


def downgrade() -> None:
    if table_exists("files") and column_exists("files", "workspace_cleanup_state"):
        if check_constraint_exists("files", "ck_files_workspace_cleanup_state"):
            op.drop_constraint("ck_files_workspace_cleanup_state", "files", type_="check")
        op.drop_column("files", "workspace_cleanup_state")
