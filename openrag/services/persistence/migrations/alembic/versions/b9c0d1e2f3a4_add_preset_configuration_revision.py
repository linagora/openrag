"""add a transaction-ordered revision for pipeline presets

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import table_exists

# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: str | Sequence[str] | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REVISION_TABLE = "preset_configuration_revision"
_TRIGGER = "trg_pipeline_presets_revision"
_FUNCTION = "bump_pipeline_presets_revision"


def upgrade() -> None:
    if not table_exists("pipeline_presets"):
        return

    if not table_exists(_REVISION_TABLE):
        op.create_table(
            _REVISION_TABLE,
            sa.Column("singleton", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
            sa.CheckConstraint("singleton", name="ck_preset_configuration_revision_singleton"),
            sa.PrimaryKeyConstraint("singleton"),
        )
    op.execute(sa.text(f"INSERT INTO {_REVISION_TABLE} (singleton) VALUES (true) ON CONFLICT DO NOTHING"))
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_FUNCTION}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                UPDATE {_REVISION_TABLE} SET revision = revision + 1 WHERE singleton;
                RETURN NULL;
            END;
            $$
            """
        )
    )
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON pipeline_presets"))
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_TRIGGER}
            AFTER INSERT OR UPDATE OR DELETE ON pipeline_presets
            FOR EACH STATEMENT EXECUTE FUNCTION {_FUNCTION}()
            """
        )
    )


def downgrade() -> None:
    if table_exists("pipeline_presets"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON pipeline_presets"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FUNCTION}()"))
    if table_exists(_REVISION_TABLE):
        op.drop_table(_REVISION_TABLE)
