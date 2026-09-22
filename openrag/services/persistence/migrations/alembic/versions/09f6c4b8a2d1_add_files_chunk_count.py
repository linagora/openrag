"""add files chunk count

Revision ID: 09f6c4b8a2d1
Revises: 1f53920217de
Create Date: 2026-09-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import check_constraint_exists, column_exists

revision: str = "09f6c4b8a2d1"
down_revision: str | Sequence[str] | None = "1f53920217de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_NAME = "ck_files_chunk_count_non_negative"


def upgrade() -> None:
    if not column_exists("files", "chunk_count"):
        op.add_column("files", sa.Column("chunk_count", sa.Integer(), nullable=True))
    if not check_constraint_exists("files", _CHECK_NAME):
        op.create_check_constraint(_CHECK_NAME, "files", "chunk_count >= 0")


def downgrade() -> None:
    if check_constraint_exists("files", _CHECK_NAME):
        op.drop_constraint(_CHECK_NAME, "files", type_="check")
    if column_exists("files", "chunk_count"):
        op.drop_column("files", "chunk_count")
