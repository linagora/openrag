"""allow speech-to-text endpoints in the model registry

Revision ID: a8b9c0d1e2f3
Revises: f6a7b8c9d0e1
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_MODEL_TYPE_IN = "model_type IN ('embedder','reranker','llm','vlm')"
_MODEL_TYPE_IN = "model_type IN ('embedder','reranker','llm','vlm','stt')"


def _model_type_constraint_sql() -> str | None:
    constraints = inspect(op.get_bind()).get_check_constraints("model_endpoints")
    return next(
        (constraint.get("sqltext") for constraint in constraints if constraint.get("name") == "ck_model_endpoint_type"),
        None,
    )


def _replace_model_type_constraint(sql: str) -> None:
    if _model_type_constraint_sql() is not None:
        op.drop_constraint("ck_model_endpoint_type", "model_endpoints", type_="check")
    op.create_check_constraint("ck_model_endpoint_type", "model_endpoints", sql)


def upgrade() -> None:
    if not table_exists("model_endpoints"):
        return
    if "'stt'" not in (_model_type_constraint_sql() or ""):
        _replace_model_type_constraint(_MODEL_TYPE_IN)


def downgrade() -> None:
    if not table_exists("model_endpoints"):
        return
    if "'stt'" in (_model_type_constraint_sql() or ""):
        # The previous CHECK constraint cannot coexist with STT rows. A
        # downgrade explicitly discards the feature's rows before restoring it.
        op.execute(sa.text("DELETE FROM model_endpoints WHERE model_type = 'stt'"))
        _replace_model_type_constraint(_PREVIOUS_MODEL_TYPE_IN)
