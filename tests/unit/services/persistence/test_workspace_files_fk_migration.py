"""Tests for the workspace_files.file_id FK migration.

The migration removes rows it cannot resolve. It must never do so silently:
every removed row is copied into a quarantine table, the count is logged, and
downgrade() puts the rows back.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.f1a2b3c4d5e6_add_workspace_files_file_id_fk",
    )


class _FakeResult:
    rowcount = 7


class _FakeBind:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def execute(self, statement, params=None):
        self.calls.append(str(statement))
        return _FakeResult()


class _FakeOp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_bind(self):
        return _FakeBind(self.calls)

    def execute(self, statement) -> None:
        self.calls.append(str(statement))

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(f"{name}{args}")

        return _record


def _run(monkeypatch, migration, func, **helpers):
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "column_type_is", lambda *_: False)
    for helper in ("column_exists", "index_exists", "fk_exists", "unique_constraint_exists", "table_exists"):
        monkeypatch.setattr(migration, helper, helpers.get(helper, lambda *_: True))
    func()
    return "\n".join(fake_op.calls)


def test_upgrade_quarantines_instead_of_deleting(monkeypatch, migration) -> None:
    statements = _run(monkeypatch, migration, migration.upgrade)

    assert f"CREATE TABLE IF NOT EXISTS {migration.ORPHAN_TABLE}" in statements
    # Both purges copy the rows out before dropping them, in one statement.
    assert statements.count(f"INSERT INTO {migration.ORPHAN_TABLE}") == 2
    assert "NOT EXISTS (SELECT 1 FROM files f WHERE f.file_id = wf.file_id)" in statements
    assert "wf.file_fk IS NULL" in statements
    # No bare delete survives.
    assert "DELETE FROM workspace_files WHERE" not in statements


def test_upgrade_logs_the_removed_row_count(monkeypatch, migration, caplog) -> None:
    with caplog.at_level("INFO", logger="alembic.runtime.migration"):
        _run(monkeypatch, migration, migration.upgrade)

    assert [r.getMessage() for r in caplog.records] == [
        f"workspace_files: quarantined 7 row(s) in {migration.ORPHAN_TABLE} (no_matching_file)",
        f"workspace_files: quarantined 7 row(s) in {migration.ORPHAN_TABLE} (unresolved_partition)",
    ]


def test_upgrade_is_a_no_op_when_already_migrated(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "column_type_is", lambda *_: True)

    migration.upgrade()

    assert fake_op.calls == []


def test_downgrade_restores_the_quarantined_rows(monkeypatch, migration) -> None:
    statements = _run(monkeypatch, migration, migration.downgrade)

    assert f"SELECT o.workspace_id, o.file_id FROM {migration.ORPHAN_TABLE} o" in statements
    assert "ON CONFLICT ON CONSTRAINT uix_workspace_file DO NOTHING" in statements
    assert f"DROP TABLE {migration.ORPHAN_TABLE}" in statements


def test_downgrade_skips_the_restore_when_nothing_was_quarantined(monkeypatch, migration) -> None:
    statements = _run(monkeypatch, migration, migration.downgrade, table_exists=lambda *_: False)

    assert migration.ORPHAN_TABLE not in statements
