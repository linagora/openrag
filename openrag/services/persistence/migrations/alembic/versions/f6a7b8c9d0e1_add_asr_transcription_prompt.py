"""add ASR transcription prompt type

The prompt library initially supported generation, retrieval, and document
enrichment prompts. Audio transcription is global rather than partition-scoped,
but it needs the same live editing and safe default behaviour.

Revision ID: f6a7b8c9d0e1
Revises: e8f9a0b1c2d3
Create Date: 2026-08-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_PROMPT_TYPE_IN = (
    "prompt_type IN ("
    "'sys_prompt','query_contextualizer','chunk_contextualizer','image_captioning',"
    "'hyde','multi_query','spoken_style_answer','topic_tagger')"
)
_PROMPT_TYPE_IN = (
    "prompt_type IN ("
    "'sys_prompt','query_contextualizer','chunk_contextualizer','image_captioning',"
    "'hyde','multi_query','spoken_style_answer','topic_tagger','asr_transcription')"
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
    if "asr_transcription" not in (_prompt_type_constraint_sql() or ""):
        _replace_prompt_type_constraint(_PROMPT_TYPE_IN)


def downgrade() -> None:
    if not table_exists("prompts"):
        return
    op.execute(sa.text("DELETE FROM prompts WHERE prompt_type = 'asr_transcription'"))
    if "asr_transcription" in (_prompt_type_constraint_sql() or ""):
        _replace_prompt_type_constraint(_PREVIOUS_PROMPT_TYPE_IN)
