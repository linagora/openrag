"""add jobs table

Revision ID: c7d8e9f0a1b2
Revises: b9c0d1e2f3a4
Create Date: 2026-09-07 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import index_exists, table_exists

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "b9c0d1e2f3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEXES: tuple[tuple[str, list], ...] = (
    ("ix_jobs_status_created_at", ["status", "created_at"]),
    ("ix_jobs_user_status", ["user_id", "status"]),
    # Expression index: the retention sweep filters and orders on
    # ``COALESCE(completed_at, created_at)``, not on ``completed_at`` alone.
    ("ix_jobs_settled_at", [sa.text("COALESCE(completed_at, created_at)")]),
    ("ix_jobs_partition_file_id", ["partition", "file_id"]),
)


def upgrade() -> None:
    """Create the durable indexing-job table.

    Idempotent: ``Base.metadata.create_all()`` runs at startup, so a freshly
    bootstrapped database already has ``jobs`` before alembic reaches this
    revision, and an unguarded CREATE TABLE would raise ``DuplicateTable``.
    """
    if not table_exists("jobs"):
        op.create_table(
            "jobs",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("partition", sa.String(), nullable=False),
            sa.Column("file_id", sa.String(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            # Frozen copy of DocumentStatus as of this revision. A new state has
            # to arrive with its own migration, which is what the parity test in
            # tests/unit/services/persistence/test_jobs_migration.py enforces.
            sa.CheckConstraint(
                "status IN ('QUEUED','SERIALIZING','COMPLETED','FAILED','CANCELLED')",
                name="ck_jobs_status",
            ),
            # Left unnamed so this CREATE TABLE and the startup
            # ``metadata.create_all()`` converge on the same Postgres-default
            # name (``jobs_user_id_fkey``).
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        )

    for name, columns in _INDEXES:
        if not index_exists("jobs", name):
            op.create_index(name, "jobs", columns)


def downgrade() -> None:
    for name, _ in _INDEXES:
        if index_exists("jobs", name):
            op.drop_index(name, table_name="jobs")
    if table_exists("jobs"):
        op.drop_table("jobs")
