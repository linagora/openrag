"""The jobs migration must be re-runnable on a database that already has it."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from core.models.catalog import DocumentStatus


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.c7d8e9f0a1b2_add_jobs",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.created_tables: list[str] = []
        self.created_indexes: list[str] = []
        self.constraints: list = []

    def create_table(self, name, *columns) -> None:
        self.created_tables.append(name)
        self.constraints.extend(c for c in columns if isinstance(c, sa.CheckConstraint | sa.ForeignKeyConstraint))

    def create_index(self, name, table, columns) -> None:
        self.created_indexes.append(name)


def _install(monkeypatch, migration, *, exists: bool):
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: exists)
    monkeypatch.setattr(migration, "index_exists", lambda _table, _index: exists)
    return op


def test_migration_follows_the_previous_head(migration):
    assert migration.revision == "c7d8e9f0a1b2"
    assert migration.down_revision == "b9c0d1e2f3a4"


def test_upgrade_creates_the_table_and_its_indexes(monkeypatch, migration):
    op = _install(monkeypatch, migration, exists=False)

    migration.upgrade()

    assert op.created_tables == ["jobs"]
    assert op.created_indexes == [
        "ix_jobs_status_created_at",
        "ix_jobs_user_status",
        "ix_jobs_settled_at",
        "ix_jobs_partition_file_id",
    ]


def test_upgrade_is_a_no_op_when_the_table_is_already_there(monkeypatch, migration):
    op = _install(monkeypatch, migration, exists=True)

    migration.upgrade()

    assert op.created_tables == []
    assert op.created_indexes == []


def test_the_status_check_still_matches_the_state_machine(monkeypatch, migration):
    """The constraint is a frozen copy, so a new state needs its own migration.

    A row carrying a status DocumentStatus does not know raises in
    ``PgJobRepository._row_to_job``, so widening the enum without widening the
    database turns a write into an unreadable row.
    """
    op = _install(monkeypatch, migration, exists=False)

    migration.upgrade()

    (check,) = [c for c in op.constraints if isinstance(c, sa.CheckConstraint)]
    pinned = {value.strip().strip("'") for value in str(check.sqltext).split("(", 1)[1].rstrip(")").split(",")}
    assert pinned == {status.value for status in DocumentStatus}


def test_user_id_is_nulled_rather_than_cascaded(monkeypatch, migration):
    """A job row is a historical record: deleting a user must not delete history."""
    op = _install(monkeypatch, migration, exists=False)

    migration.upgrade()

    (fk,) = [c for c in op.constraints if isinstance(c, sa.ForeignKeyConstraint)]
    assert fk.ondelete == "SET NULL"
