"""add_endpoints_presets_phase14

Revision ID: 06dd2101ea3a
Revises: f5b6c918f741
Create Date: 2026-06-04 10:24:14.741985

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, index_exists, table_exists
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "06dd2101ea3a"
down_revision: str | Sequence[str] | None = "f5b6c918f741"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not table_exists("model_endpoints"):
        op.create_table(
            "model_endpoints",
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("model_type", sa.String(), nullable=False),
            sa.Column("endpoint", sa.String(), nullable=False),
            sa.Column("model_name", sa.String(), nullable=True),
            sa.Column("batch_size", sa.Integer(), server_default="32", nullable=False),
            sa.Column("timeout", sa.Float(), server_default="30.0", nullable=False),
            sa.Column(
                "extra",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
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
            sa.CheckConstraint(
                "model_type IN ('embedder','reranker','llm','vlm')",
                name="ck_model_endpoint_type",
            ),
            sa.PrimaryKeyConstraint("name", "model_type"),
        )

    if not table_exists("pipeline_presets"):
        op.create_table(
            "pipeline_presets",
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("preset_type", sa.String(), nullable=False),
            sa.Column(
                "config",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
            ),
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
            sa.CheckConstraint(
                "preset_type IN ('indexation','retrieval')",
                name="ck_pipeline_preset_type",
            ),
            sa.PrimaryKeyConstraint("name", "preset_type"),
        )

    if not column_exists("files", "indexation_config"):
        op.add_column(
            "files",
            sa.Column(
                "indexation_config",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )

    _partition_columns = {
        "description": sa.Column("description", sa.String(), server_default=sa.text("''"), nullable=False),
        "embedder": sa.Column(
            "embedder",
            sa.String(),
            server_default=sa.text("'default'"),
            nullable=False,
        ),
        "indexation_preset": sa.Column(
            "indexation_preset",
            sa.String(),
            server_default=sa.text("'default'"),
            nullable=False,
        ),
        "retrieval_preset": sa.Column(
            "retrieval_preset",
            sa.String(),
            server_default=sa.text("'default'"),
            nullable=False,
        ),
        "dimension": sa.Column("dimension", sa.Integer(), server_default="1024", nullable=False),
        "collection_name": sa.Column("collection_name", sa.String(), nullable=True),
        "chat_history_depth": sa.Column("chat_history_depth", sa.Integer(), server_default="0", nullable=False),
        "chat_llm": sa.Column("chat_llm", sa.String(), nullable=True),
        "updated_at": sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    }
    for col_name, col_def in _partition_columns.items():
        if not column_exists("partitions", col_name):
            op.add_column("partitions", col_def)

    if not index_exists("workspaces", "ix_workspaces_partition_name"):
        op.create_index(
            op.f("ix_workspaces_partition_name"),
            "workspaces",
            ["partition_name"],
            unique=False,
        )


def downgrade() -> None:
    if index_exists("workspaces", "ix_workspaces_partition_name"):
        op.drop_index(op.f("ix_workspaces_partition_name"), table_name="workspaces")

    for col in [
        "updated_at",
        "chat_llm",
        "chat_history_depth",
        "collection_name",
        "dimension",
        "retrieval_preset",
        "indexation_preset",
        "embedder",
        "description",
    ]:
        if column_exists("partitions", col):
            op.drop_column("partitions", col)

    if column_exists("files", "indexation_config"):
        op.drop_column("files", "indexation_config")

    if table_exists("pipeline_presets"):
        op.drop_table("pipeline_presets")

    if table_exists("model_endpoints"):
        op.drop_table("model_endpoints")
