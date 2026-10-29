"""add durable per-task indexing stage history

Revision ID: bcbdf503a6f1
Revises: 5e6d7c8b9a01
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import check_constraint_exists, column_exists, index_exists, table_exists
from sqlalchemy.dialects import postgresql

revision: str = "bcbdf503a6f1"
down_revision: str | Sequence[str] | None = "5e6d7c8b9a01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_INDEXES = (
    (
        "uq_job_stage_events_one_start",
        ["task_id", "attempt", "invocation_id"],
        "status = 'started'",
    ),
    (
        "uq_job_stage_events_one_terminal",
        ["task_id", "attempt", "invocation_id"],
        "status IN ('completed', 'failed', 'cancelled')",
    ),
)


def _create_events_table() -> None:
    if table_exists("job_stage_events"):
        return
    op.create_table(
        "job_stage_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("invocation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage_id", sa.String(length=64), nullable=False),
        sa.Column("stage_rank", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("failure_effect", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("task_id", "attempt", "sequence", name="uq_job_stage_events_task_attempt_sequence"),
        sa.CheckConstraint("attempt > 0", name="ck_job_stage_events_attempt_positive"),
        sa.CheckConstraint("sequence > 0", name="ck_job_stage_events_sequence_positive"),
        sa.CheckConstraint(
            "status IN ('started', 'completed', 'failed', 'cancelled')",
            name="ck_job_stage_events_status",
        ),
        sa.CheckConstraint("stage_rank IS NULL OR stage_rank >= 0", name="ck_job_stage_events_rank_nonnegative"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_job_stage_events_duration_nonnegative"),
        sa.CheckConstraint(
            "status <> 'started' OR duration_ms IS NULL",
            name="ck_job_stage_events_started_duration_null",
        ),
        sa.CheckConstraint(
            "failure_effect IS NULL OR failure_effect IN ('continued_degraded', 'task_failed')",
            name="ck_job_stage_events_failure_effect",
        ),
        sa.CheckConstraint("failure_effect IS NULL OR status = 'failed'", name="ck_job_stage_events_failure_status"),
    )


def _create_stage_state_table() -> None:
    if table_exists("job_stage_state"):
        return
    op.create_table(
        "job_stage_state",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("latest_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("snapshot_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("tracking_version", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(length=24), server_default="preparing", nullable=False),
        sa.Column("current_stage_id", sa.String(length=64), nullable=True),
        sa.Column("current_stage_rank", sa.Integer(), nullable=True),
        sa.Column("position_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stage_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_invocation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("interrupted_invocation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("persisted_through_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("expected_final_sequence", sa.BigInteger(), nullable=True),
        sa.Column("known_loss", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("known_conflict", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("has_open_invocation", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("history_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("has_summary", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
        sa.CheckConstraint("attempt > 0", name="ck_job_stage_state_attempt_positive"),
        sa.CheckConstraint("latest_sequence >= 0", name="ck_job_stage_state_sequence_nonnegative"),
        sa.CheckConstraint("snapshot_revision >= 0", name="ck_job_stage_state_revision_nonnegative"),
        sa.CheckConstraint(
            "tracking_version IS NULL OR tracking_version = 1",
            name="ck_job_stage_state_tracking_version",
        ),
        sa.CheckConstraint(
            "phase IN ('queued', 'preparing', 'stage', 'between_stages', 'finalizing')",
            name="ck_job_stage_state_phase",
        ),
        sa.CheckConstraint(
            "persisted_through_sequence >= 0",
            name="ck_job_stage_state_persisted_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            "expected_final_sequence IS NULL OR expected_final_sequence >= 0",
            name="ck_job_stage_state_expected_final_nonnegative",
        ),
    )


def upgrade() -> None:
    if table_exists("jobs") and not column_exists("jobs", "stage_tracking_version"):
        op.add_column("jobs", sa.Column("stage_tracking_version", sa.Integer(), nullable=True))
    if table_exists("jobs") and not check_constraint_exists("jobs", "ck_jobs_stage_tracking_version"):
        op.create_check_constraint(
            "ck_jobs_stage_tracking_version",
            "jobs",
            "stage_tracking_version IS NULL OR stage_tracking_version IN (0, 1)",
        )
    if table_exists("jobs") and column_exists("jobs", "stage_tracking_version"):
        # All terminal rows that predate this rollout came from the
        # uninstrumented worker. Active and mixed-version rows remain unknown.
        op.execute(
            """
            UPDATE jobs
            SET stage_tracking_version = 0
            WHERE stage_tracking_version IS NULL
              AND status IN ('COMPLETED', 'FAILED', 'CANCELLED')
            """
        )

    _create_events_table()
    for name, columns, predicate in _EVENT_INDEXES:
        if not index_exists("job_stage_events", name):
            op.create_index(
                name,
                "job_stage_events",
                columns,
                unique=True,
                postgresql_where=sa.text(predicate),
            )
    _create_stage_state_table()


def downgrade() -> None:
    if table_exists("job_stage_state"):
        op.drop_table("job_stage_state")
    if table_exists("job_stage_events"):
        op.drop_table("job_stage_events")
    if table_exists("jobs") and check_constraint_exists("jobs", "ck_jobs_stage_tracking_version"):
        op.drop_constraint("ck_jobs_stage_tracking_version", "jobs", type_="check")
    if table_exists("jobs") and column_exists("jobs", "stage_tracking_version"):
        op.drop_column("jobs", "stage_tracking_version")
