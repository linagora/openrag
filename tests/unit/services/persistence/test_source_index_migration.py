"""The source-index migration is guarded both ways, so it re-runs after create_all."""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.9ed228e7ea17_index_files_source",
    )


class _FakeContext:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    @contextmanager
    def autocommit_block(self):
        self.calls.append("autocommit")
        yield
        self.calls.append("end autocommit")


class _FakeOp:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.context = _FakeContext(self.calls)

    def get_context(self):
        return self.context

    def execute(self, statement) -> None:
        self.calls.append(str(statement))


def _install(monkeypatch, migration, *, validity: bool | None, table: bool = True) -> _FakeOp:
    op = _FakeOp()
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: table)
    monkeypatch.setattr(migration, "_index_validity", lambda: validity)
    return op


CREATE = "CREATE INDEX CONCURRENTLY ix_files_source ON files ((file_metadata ->> 'source'))"
DROP = "DROP INDEX CONCURRENTLY ix_files_source"


def test_chains_after_the_file_fences_revision(migration):
    assert migration.down_revision == "a2b3c4d5e6f7"


def test_upgrade_builds_the_index_concurrently_outside_the_transaction(monkeypatch, migration):
    op = _install(monkeypatch, migration, validity=None)
    migration.upgrade()
    assert op.calls == ["autocommit", CREATE, "end autocommit"]


def test_upgrade_keeps_an_index_create_all_made(monkeypatch, migration):
    op = _install(monkeypatch, migration, validity=True)
    migration.upgrade()
    assert op.calls == []


def test_upgrade_rebuilds_an_index_a_failed_build_left_invalid(monkeypatch, migration):
    op = _install(monkeypatch, migration, validity=False)
    migration.upgrade()
    assert op.calls == ["autocommit", DROP, "end autocommit", "autocommit", CREATE, "end autocommit"]


def test_upgrade_without_the_table_does_nothing(monkeypatch, migration):
    op = _install(monkeypatch, migration, validity=None, table=False)
    migration.upgrade()
    assert op.calls == []


@pytest.mark.parametrize("validity", [True, False])
def test_downgrade_drops_the_index_valid_or_not(monkeypatch, migration, validity):
    op = _install(monkeypatch, migration, validity=validity)
    migration.downgrade()
    assert op.calls == ["autocommit", DROP, "end autocommit"]


def test_downgrade_without_the_index_does_nothing(monkeypatch, migration):
    op = _install(monkeypatch, migration, validity=None)
    migration.downgrade()
    assert op.calls == []


def test_the_schema_declares_the_same_index():
    from services.persistence.schema import files
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex

    index = next(i for i in files.indexes if i.name == "ix_files_source")
    assert str(CreateIndex(index).compile(dialect=postgresql.dialect())) == (
        "CREATE INDEX ix_files_source ON files ((file_metadata ->> 'source'))"
    )
