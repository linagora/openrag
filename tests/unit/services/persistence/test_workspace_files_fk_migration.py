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
    def __init__(self, rowcount) -> None:
        self.rowcount = rowcount


class _FakeScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one(self):
        return self.value


class _FakeBind:
    """Pretends every quarantine statement touched `rowcount` rows.

    `quarantined` is what ``SELECT COUNT(*) FROM <orphan table>`` reports in
    downgrade(); equal to `rowcount` means every row made it back.
    """

    def __init__(self, calls: list[str], rowcount: int = 7, quarantined: int | None = None) -> None:
        self.calls = calls
        self.rowcount = rowcount
        self.quarantined = rowcount if quarantined is None else quarantined

    def execute(self, statement, params=None):
        statement = str(statement)
        self.calls.append(statement)
        if "file_id IS NULL" in statement:
            return _FakeScalarResult(self.null_file_id_count)
        if statement.startswith("SELECT COUNT(*)"):
            return _FakeScalarResult(self.quarantined)
        return _FakeResult(self.rowcount)

    null_file_id_count = 0


class _NullFileIdBind(_FakeBind):
    null_file_id_count = 1


class _FakeOp:
    bind_class = _FakeBind

    def __init__(self, quarantined: int | None = None) -> None:
        self.calls: list[str] = []
        self.quarantined = quarantined

    def get_bind(self):
        return self.bind_class(self.calls, quarantined=self.quarantined)

    def execute(self, statement) -> None:
        self.calls.append(str(statement))

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(f"{name}{args}")

        return _record


class _NullFileIdOp(_FakeOp):
    bind_class = _NullFileIdBind


def _run(monkeypatch, migration, func, quarantined=None, **helpers):
    fake_op = _FakeOp(quarantined=quarantined)
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "column_type_is", lambda *_: False)
    for helper in ("column_exists", "index_exists", "fk_exists", "unique_constraint_exists", "table_exists"):
        monkeypatch.setattr(migration, helper, helpers.get(helper, lambda *_: True))
    func()
    return "\n".join(fake_op.calls)


def test_upgrade_quarantines_instead_of_deleting(monkeypatch, migration) -> None:
    statements = _run(monkeypatch, migration, migration.upgrade, table_exists=lambda *_: False)

    assert f"create_table('{migration.ORPHAN_TABLE}'" in statements
    # Both purges copy the rows out before dropping them, in one statement.
    assert statements.count(f"INSERT INTO {migration.ORPHAN_TABLE}") == 2
    assert "NOT EXISTS (SELECT 1 FROM files f WHERE f.file_id = wf.file_id)" in statements
    assert "wf.file_fk IS NULL" in statements
    # No bare delete survives.
    assert "DELETE FROM workspace_files WHERE" not in statements


def test_upgrade_skips_the_quarantine_table_when_it_already_exists(monkeypatch, migration) -> None:
    statements = _run(monkeypatch, migration, migration.upgrade, table_exists=lambda *_: True)

    assert "create_table(" not in statements


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


def test_upgrade_rejects_null_file_ids_before_mutating_schema(monkeypatch, migration) -> None:
    fake_op = _NullFileIdOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "column_type_is", lambda *_: False)
    monkeypatch.setattr(migration, "table_exists", lambda *_: False)
    monkeypatch.setattr(migration, "column_exists", lambda *_: False)
    monkeypatch.setattr(migration, "index_exists", lambda *_: False)
    monkeypatch.setattr(migration, "fk_exists", lambda *_: False)
    monkeypatch.setattr(migration, "unique_constraint_exists", lambda *_: False)

    with pytest.raises(RuntimeError, match=r"1 row\(s\) have NULL file_id"):
        migration.upgrade()

    assert all("CREATE TABLE" not in call for call in fake_op.calls)


def test_downgrade_restores_the_quarantined_rows(monkeypatch, migration) -> None:
    statements = _run(monkeypatch, migration, migration.downgrade)

    assert f"SELECT o.workspace_id, o.file_id FROM {migration.ORPHAN_TABLE} o" in statements
    assert "ON CONFLICT ON CONSTRAINT uix_workspace_file DO NOTHING" in statements
    assert f"DROP TABLE {migration.ORPHAN_TABLE}" in statements


def test_downgrade_keeps_the_table_when_a_row_cannot_be_restored(monkeypatch, migration, caplog) -> None:
    # 9 rows quarantined, 7 restored: the other 2 belong to workspaces deleted
    # since the upgrade, or collided on the unique constraint. Dropping the table
    # now would destroy the only copy left of them.
    with caplog.at_level("WARNING", logger="alembic.runtime.migration"):
        statements = _run(monkeypatch, migration, migration.downgrade, quarantined=9)

    assert f"DROP TABLE {migration.ORPHAN_TABLE}" not in statements
    assert [r.getMessage() for r in caplog.records] == [
        f"workspace_files: restored 7 of 9 quarantined row(s); keeping {migration.ORPHAN_TABLE} "
        f"so the remaining 2 row(s) are not lost"
    ]


def test_downgrade_skips_the_restore_when_nothing_was_quarantined(monkeypatch, migration) -> None:
    statements = _run(monkeypatch, migration, migration.downgrade, table_exists=lambda *_: False)

    assert migration.ORPHAN_TABLE not in statements
