"""drop the fields of the removed multiQuery and hyde retrievers from retrieval presets

``type``, ``llm``, ``hyde_prompt_name`` and ``multi_query_prompt_name`` configured
the two retrievers that were removed; retrieval ignores them. Removing them from
the stored presets keeps the admin UI and ``GET /presets`` from showing settings
that do nothing.

Revision ID: d9e2e59a7a4e
Revises: a2b3c4d5e6f7
Create Date: 2026-11-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from schema_helpers import column_exists, table_exists

# revision identifiers, used by Alembic.
revision: str = "d9e2e59a7a4e"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REMOVED_FIELDS = ("type", "llm", "hyde_prompt_name", "multi_query_prompt_name")


def upgrade() -> None:
    if not table_exists("pipeline_presets") or not column_exists("pipeline_presets", "config"):
        return
    removed = "'{" + ",".join(REMOVED_FIELDS) + "}'::text[]"
    # Only the rows that hold one of the fields are written, so running it again changes nothing.
    op.execute(
        sa.text(
            f"""
            UPDATE pipeline_presets
            SET config = config - {removed}
            WHERE preset_type = 'retrieval'
              AND config <> config - {removed}
            """
        )
    )


def downgrade() -> None:
    """No-op: the removed fields were unused, so there is nothing to restore."""
