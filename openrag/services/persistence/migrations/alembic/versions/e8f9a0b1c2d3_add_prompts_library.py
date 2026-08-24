"""add_prompts_library

Adds DB-backed prompt management:

* ``prompts`` — the global prompt library (the menu of named prompts), with a
  partial-unique index enforcing at most one ``is_default`` row per prompt type.
* ``partitions.generation_prompt_names`` — a JSONB map ``{prompt_type: name}``
  naming the library prompt a partition uses for each generation prompt
  (``sys_prompt``, ``spoken_style_answer``). Indexation/retrieval prompts are
  named on their presets (JSONB config, no schema change).

Idempotent: every op is guarded by an inspector check so re-application against
a database that already contains these objects (an older or partially-migrated
deployment) is a safe no-op — matching the guarded style of the other
migrations in this tree.

Revision ID: e8f9a0b1c2d3
Revises: e5f6a7b8c9d0
Create Date: 2026-07-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e8f9a0b1c2d3"
# Chained after e5f6a7b8c9d0 rather than its original parent d4e5f6a7b8c9:
# that revision landed on develop while this branch was open and took the same
# parent, and two siblings would leave the tree with multiple heads — which
# aborts ``alembic upgrade head`` at boot and takes the whole app down, not just
# this feature.
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in lockstep with schema._PROMPT_TYPE_VALUES / core.models.prompt.PromptType.
_PROMPT_TYPE_IN = (
    "prompt_type IN ("
    "'sys_prompt','query_contextualizer','chunk_contextualizer','image_captioning',"
    "'hyde','multi_query','spoken_style_answer','topic_tagger')"
)


def upgrade() -> None:
    if not table_exists("prompts"):
        op.create_table(
            "prompts",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("prompt_type", sa.String(), nullable=False),
            sa.Column("name", sa.String(), server_default=sa.text("''"), nullable=False),
            sa.Column("content", sa.String(), nullable=False),
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
            sa.CheckConstraint(_PROMPT_TYPE_IN, name="ck_prompt_type"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_prompts_type", "prompts", ["prompt_type"])
        # Name is the selection key — unique per type.
        op.create_index("uix_prompts_type_name", "prompts", ["prompt_type", "name"], unique=True)
        # At most one global default per type.
        op.create_index(
            "uix_prompts_default_per_type",
            "prompts",
            ["prompt_type"],
            unique=True,
            postgresql_where=sa.text("is_default = true"),
        )

    if not column_exists("partitions", "generation_prompt_names"):
        op.add_column(
            "partitions",
            sa.Column(
                "generation_prompt_names",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if column_exists("partitions", "generation_prompt_names"):
        op.drop_column("partitions", "generation_prompt_names")
    if table_exists("prompts"):
        op.drop_table("prompts")
