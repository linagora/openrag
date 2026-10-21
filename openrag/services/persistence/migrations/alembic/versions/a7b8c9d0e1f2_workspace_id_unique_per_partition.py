"""make workspace_id unique per partition instead of globally

Two partitions may now each own a workspace with the same ``workspace_id``.
That has a knock-on effect on the join table: ``workspace_files.workspace_id``
used to be a string foreign key onto ``workspaces.workspace_id``, which only
works while that column is unique. The join now references the integer
``workspaces.id`` instead, mirroring how ``workspace_files.file_id`` already
references ``files.id`` rather than the non-unique ``files.file_id`` string.

Steps, in dependency order (the old FK must go before the unique index it
points at):

1. ``workspace_files.workspace_id``: string → integer FK on ``workspaces.id``.
2. ``workspaces.workspace_id``: drop the global unique index, keep a plain one.
3. Add ``UNIQUE (partition_name, workspace_id)``.

Idempotent: a freshly bootstrapped database already has the target shape
(``Base.metadata.create_all()`` runs before Alembic), so every step checks
the current state first.

Revision ID: a7b8c9d0e1f2
Revises: d5e6f7a8b9c0
Create Date: 2026-09-23

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from services.persistence.migrations.alembic.schema_helpers import (
    column_exists,
    column_type_is,
    fk_exists,
    foreign_keys_to,
    index_exists,
    index_is_unique,
    unique_constraint_exists,
    unique_constraints_on,
)

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

WORKSPACE_FILES_FK = "fk_workspace_files_workspace_id"
WORKSPACE_FILES_UNIQUE = "uix_workspace_file"
WORKSPACE_FILES_INDEX = "ix_workspace_files_workspace_id"
WORKSPACES_INDEX = "ix_workspaces_workspace_id"
WORKSPACES_UNIQUE = "uix_workspace_partition_id"


def _drop_workspace_files_join_objects() -> None:
    """Drop the FK, unique constraint and index built on ``workspace_files.workspace_id``."""
    for fk_name in foreign_keys_to("workspace_files", "workspaces"):
        op.drop_constraint(fk_name, "workspace_files", type_="foreignkey")
    if unique_constraint_exists("workspace_files", WORKSPACE_FILES_UNIQUE):
        op.drop_constraint(WORKSPACE_FILES_UNIQUE, "workspace_files", type_="unique")
    if index_exists("workspace_files", WORKSPACE_FILES_INDEX):
        op.drop_index(WORKSPACE_FILES_INDEX, table_name="workspace_files")


def _recreate_workspace_files_join_objects(*, referred_column: str) -> None:
    if not index_exists("workspace_files", WORKSPACE_FILES_INDEX):
        op.create_index(WORKSPACE_FILES_INDEX, "workspace_files", ["workspace_id"])
    if not unique_constraint_exists("workspace_files", WORKSPACE_FILES_UNIQUE):
        op.create_unique_constraint(WORKSPACE_FILES_UNIQUE, "workspace_files", ["workspace_id", "file_id"])
    if not fk_exists("workspace_files", WORKSPACE_FILES_FK):
        op.create_foreign_key(
            WORKSPACE_FILES_FK,
            "workspace_files",
            "workspaces",
            ["workspace_id"],
            [referred_column],
            ondelete="CASCADE",
        )


def upgrade() -> None:
    # 1. workspace_files.workspace_id: string workspaces.workspace_id → integer workspaces.id
    if not column_type_is("workspace_files", "workspace_id", sa.Integer):
        _drop_workspace_files_join_objects()

        if not column_exists("workspace_files", "workspace_fk"):
            op.add_column("workspace_files", sa.Column("workspace_fk", sa.Integer(), nullable=True))
        if column_exists("workspace_files", "workspace_id"):
            # workspace_id is still globally unique at this point, so the join is unambiguous.
            op.execute(
                "UPDATE workspace_files wf SET workspace_fk = w.id "
                "FROM workspaces w WHERE w.workspace_id = wf.workspace_id"
            )
            # The old FK made this impossible, but a deployment that lost it would
            # otherwise fail on the NOT NULL below. Such a row references no
            # workspace, which is exactly what ON DELETE CASCADE would have removed.
            dangling = op.get_bind().execute(sa.text("DELETE FROM workspace_files WHERE workspace_fk IS NULL")).rowcount
            if dangling:
                logger.warning("workspace_files: removed %s row(s) referencing no workspace", dangling)
            op.drop_column("workspace_files", "workspace_id")
        if column_exists("workspace_files", "workspace_fk"):
            op.alter_column("workspace_files", "workspace_fk", new_column_name="workspace_id", nullable=False)
        _recreate_workspace_files_join_objects(referred_column="id")

    # 2. workspaces.workspace_id: no longer unique on its own.
    #    ``Column(unique=True, index=True)`` materialises as a unique *index*; a
    #    plain ``unique=True`` would have been a constraint. Handle both.
    if index_is_unique("workspaces", WORKSPACES_INDEX):
        op.drop_index(WORKSPACES_INDEX, table_name="workspaces")
    for name in unique_constraints_on("workspaces", ["workspace_id"]):
        op.drop_constraint(name, "workspaces", type_="unique")
    if not index_exists("workspaces", WORKSPACES_INDEX):
        op.create_index(WORKSPACES_INDEX, "workspaces", ["workspace_id"])

    # 3. Unique per partition.
    if not unique_constraint_exists("workspaces", WORKSPACES_UNIQUE):
        op.create_unique_constraint(WORKSPACES_UNIQUE, "workspaces", ["partition_name", "workspace_id"])


def downgrade() -> None:
    # Global uniqueness can only be restored if no id is shared across partitions.
    duplicates = (
        op.get_bind()
        .execute(sa.text("SELECT workspace_id FROM workspaces GROUP BY workspace_id HAVING COUNT(*) > 1"))
        .scalars()
        .all()
    )
    if duplicates:
        raise RuntimeError(
            "Cannot downgrade: these workspace_id values exist in more than one partition and would violate "
            f"the global unique index: {sorted(duplicates)}. Rename or delete them first."
        )

    # 1. workspace_files.workspace_id: integer workspaces.id → string workspaces.workspace_id.
    #    The old FK needs the unique index back on workspaces.workspace_id, so
    #    restore that before recreating the FK.
    if unique_constraint_exists("workspaces", WORKSPACES_UNIQUE):
        op.drop_constraint(WORKSPACES_UNIQUE, "workspaces", type_="unique")
    if index_exists("workspaces", WORKSPACES_INDEX) and not index_is_unique("workspaces", WORKSPACES_INDEX):
        op.drop_index(WORKSPACES_INDEX, table_name="workspaces")
    if not index_exists("workspaces", WORKSPACES_INDEX):
        op.create_index(WORKSPACES_INDEX, "workspaces", ["workspace_id"], unique=True)

    if not column_type_is("workspace_files", "workspace_id", sa.String):
        _drop_workspace_files_join_objects()

        if not column_exists("workspace_files", "workspace_str"):
            op.add_column("workspace_files", sa.Column("workspace_str", sa.String(), nullable=True))
        if column_exists("workspace_files", "workspace_id"):
            op.execute(
                "UPDATE workspace_files wf SET workspace_str = w.workspace_id "
                "FROM workspaces w WHERE w.id = wf.workspace_id"
            )
            op.drop_column("workspace_files", "workspace_id")
        if column_exists("workspace_files", "workspace_str"):
            op.alter_column("workspace_files", "workspace_str", new_column_name="workspace_id", nullable=False)
        _recreate_workspace_files_join_objects(referred_column="workspace_id")
