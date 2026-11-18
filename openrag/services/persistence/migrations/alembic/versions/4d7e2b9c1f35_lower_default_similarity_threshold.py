"""lower the stored default similarity threshold from 0.6 to 0.2

The threshold is a floor on the dense (semantic) side of the hybrid search only.
With the default embedder, relevant passages score 0.2-0.7, so 0.6 dropped many
of them. Presets seeded by earlier releases carry 0.6; move those to the new
default and leave any other value (chosen by an admin) untouched.

Revision ID: 4d7e2b9c1f35
Revises: f3a4b5c6d7e8
Create Date: 2026-11-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists

# revision identifiers, used by Alembic.
revision: str = "4d7e2b9c1f35"
down_revision: str | Sequence[str] | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if not table_exists("pipeline_presets") or not column_exists("pipeline_presets", "config"):
        return
    # Only an exact 0.6 on a retrieval preset is rewritten, so re-running is a no-op.
    op.execute(
        sa.text(
            """
            UPDATE pipeline_presets
            SET config = jsonb_set(config, '{similarity_threshold}', '0.2'::jsonb)
            WHERE preset_type = 'retrieval'
              AND config -> 'similarity_threshold' = '0.6'::jsonb
            """
        )
    )


def downgrade() -> None:
    """No-op: a stored 0.2 may have been chosen by an admin, so it is not turned back into 0.6."""
