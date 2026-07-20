"""add jobs table for durable indexation job state

Revision ID: d4e5f6a7b8c9
Revises: b7c1d2e3f4a5
Create Date: 2026-07-16 10:12:44.118203

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import index_exists, table_exists
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "b7c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEXES = (
    ("ix_jobs_status_created_at", ["status", "created_at"]),
    ("ix_jobs_user_status", ["user_id", "status"]),
    # Expression index: the retention sweep filters and orders on
    # ``COALESCE(completed_at, created_at)``, not on ``completed_at`` alone.
    ("ix_jobs_settled_at", [sa.text("COALESCE(completed_at, created_at)")]),
)


def upgrade() -> None:
    """Upgrade schema.

    Idempotent: ``Base.metadata.create_all()`` runs at app startup, so a freshly
    bootstrapped database already has ``jobs`` before alembic reaches this
    revision — an unguarded CREATE TABLE would raise ``DuplicateTable``.
    """
    if not table_exists("jobs"):
        op.create_table(
            "jobs",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("partition", sa.String(), nullable=False),
            sa.Column("file_id", sa.String(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column(
                "job_metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint(
                "status IN ('QUEUED','SERIALIZING','CHUNKING','INSERTING','COMPLETED','FAILED','CANCELLED')",
                name="ck_jobs_status",
            ),
            # No FK to partitions.partition: a job row is a historical record and
            # must outlive the partition it targeted.
            # Constraints stay unnamed so this CREATE TABLE and the startup
            # ``metadata.create_all()`` converge on the same Postgres-default
            # names (``jobs_pkey`` / ``jobs_user_id_fkey``).
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns in _INDEXES:
        if not index_exists("jobs", name):
            op.create_index(name, "jobs", columns)


def downgrade() -> None:
    """Downgrade schema."""
    for name, _ in _INDEXES:
        if index_exists("jobs", name):
            op.drop_index(name, table_name="jobs")
    if table_exists("jobs"):
        op.drop_table("jobs")
