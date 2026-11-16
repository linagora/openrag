"""add the agent prompt type

The ``agentic`` chat mode answers with an agent whose instructions are a
prompt of type ``agent``, seeded and edited like the others. The prompts
table's check constraint lists the types a row may have.

Revision ID: c9d0e1f2a3b4
Revises: f3a4b5c6d7e8
Create Date: 2026-11-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_PROMPT_TYPE_IN = (
    "prompt_type IN ("
    "'sys_prompt','query_contextualizer','chunk_contextualizer','image_captioning',"
    "'hyde','multi_query','spoken_style_answer','topic_tagger','asr_transcription')"
)
_PROMPT_TYPE_IN = (
    "prompt_type IN ("
    "'sys_prompt','query_contextualizer','chunk_contextualizer','image_captioning',"
    "'hyde','multi_query','spoken_style_answer','topic_tagger','asr_transcription','agent')"
)


def _prompt_type_constraint_sql() -> str | None:
    constraints = inspect(op.get_bind()).get_check_constraints("prompts")
    return next(
        (constraint.get("sqltext") for constraint in constraints if constraint.get("name") == "ck_prompt_type"), None
    )


def _replace_prompt_type_constraint(sql: str) -> None:
    if _prompt_type_constraint_sql() is not None:
        op.drop_constraint("ck_prompt_type", "prompts", type_="check")
    op.create_check_constraint("ck_prompt_type", "prompts", sql)


def upgrade() -> None:
    if not table_exists("prompts"):
        return
    # Postgres shows the constraint as ``prompt_type::text = ANY (ARRAY['agent'::character varying, ...])``.
    if "'agent'" not in (_prompt_type_constraint_sql() or ""):
        _replace_prompt_type_constraint(_PROMPT_TYPE_IN)


def downgrade() -> None:
    if not table_exists("prompts"):
        return
    op.execute(sa.text("DELETE FROM prompts WHERE prompt_type = 'agent'"))
    if "'agent'" in (_prompt_type_constraint_sql() or ""):
        _replace_prompt_type_constraint(_PREVIOUS_PROMPT_TYPE_IN)
