"""SQL revision a7b8c9d0e1f2: workspace_id unique per partition.

Drives the migration against a recording fake of ``op`` so the two things
that matter can be pinned without a database: the order of the schema ops
(the old FK depends on the unique index it references, so it has to go
first) and the idempotency guards (a freshly bootstrapped database already
has the target shape). The real SQL is exercised by
``tests/integration/repos/test_workspace_partition_migration.py``.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import sqlalchemy as sa


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.a7b8c9d0e1f2_workspace_id_unique_per_partition",
    )


class _Result:
    def __init__(self, values: list, rowcount: int = 0) -> None:
        self._values = values
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _FakeBind:
    def __init__(self, calls: list[str], *, duplicates: list[str], dangling: int) -> None:
        self._calls = calls
        self._duplicates = duplicates
        self._dangling = dangling

    def execute(self, statement, params=None):
        statement = str(statement)
        self._calls.append(statement)
        if "HAVING COUNT(*) > 1" in statement:
            return _Result(self._duplicates)
        if statement.startswith("DELETE FROM workspace_files"):
            return _Result([], rowcount=self._dangling)
        return _Result([])


class _FakeOp:
    """Records every ``op`` call; remembers what it dropped so the existence
    helpers can answer "gone" once a drop has been recorded."""

    def __init__(self, *, duplicates: list[str] | None = None, dangling: int = 0) -> None:
        self.calls: list[str] = []
        self.dropped: set[str] = set()
        # Columns of workspace_files that matter here; follows add/drop/rename.
        self.columns: set[str] = {"workspace_id"}
        self._bind = _FakeBind(self.calls, duplicates=duplicates or [], dangling=dangling)

    def get_bind(self):
        return self._bind

    def execute(self, statement) -> None:
        self.calls.append(str(statement))

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(f"{name}{args}{kwargs or ''}")
            if name in {"drop_index", "drop_constraint"}:
                self.dropped.add(args[0])
            elif name == "add_column":
                self.columns.add(args[1].name)
            elif name == "drop_column":
                self.columns.discard(args[1])
            elif name == "alter_column" and kwargs.get("new_column_name"):
                self.columns.discard(args[1])
                self.columns.add(kwargs["new_column_name"])

        return _record


def _legacy(op: _FakeOp) -> dict:
    """The inspector's answers for a database still on the previous revision:
    string join column with the auto-named FK, unique index on workspaces.workspace_id."""
    return {
        "column_type_is": lambda table, column, sa_type: sa_type is sa.String,
        "column_exists": lambda table, column: column in op.columns,
        "fk_exists": lambda *_: False,
        "foreign_keys_to": lambda *_: []
        if "workspace_files_workspace_id_fkey" in op.dropped
        else ["workspace_files_workspace_id_fkey"],
        "index_exists": lambda table, index: index in {"ix_workspace_files_workspace_id", "ix_workspaces_workspace_id"}
        and index not in op.dropped,
        "index_is_unique": lambda table, index: index == "ix_workspaces_workspace_id" and index not in op.dropped,
        "unique_constraint_exists": lambda table, name: name == "uix_workspace_file" and name not in op.dropped,
        "unique_constraints_on": lambda *_: [],
    }


def _migrated(op: _FakeOp) -> dict:
    """The inspector's answers once the target shape is in place — either
    because this migration already ran or because ``create_all`` built it."""
    return {
        "column_type_is": lambda table, column, sa_type: sa_type is sa.Integer,
        "column_exists": lambda table, column: column in op.columns,
        "fk_exists": lambda table, name: name not in op.dropped,
        "foreign_keys_to": lambda *_: []
        if "fk_workspace_files_workspace_id" in op.dropped
        else ["fk_workspace_files_workspace_id"],
        "index_exists": lambda table, index: index not in op.dropped,
        "index_is_unique": lambda *_: False,
        "unique_constraint_exists": lambda table, name: name not in op.dropped,
        "unique_constraints_on": lambda *_: [],
    }


def _run(monkeypatch, migration, func, state, **op_kwargs) -> list[str]:
    fake_op = _FakeOp(**op_kwargs)
    monkeypatch.setattr(migration, "op", fake_op)
    for helper, impl in state(fake_op).items():
        monkeypatch.setattr(migration, helper, impl)
    func()
    return fake_op.calls


def _index_of(calls: list[str], needle: str) -> int:
    return next(i for i, call in enumerate(calls) if needle in call)


def test_upgrade_converts_the_join_column_before_dropping_the_unique_index(monkeypatch, migration) -> None:
    calls = _run(monkeypatch, migration, migration.upgrade, _legacy)

    fk_drop = _index_of(calls, "drop_constraint('workspace_files_workspace_id_fkey'")
    backfill = _index_of(calls, "SET workspace_fk = w.id")
    old_column_drop = _index_of(calls, "drop_column('workspace_files', 'workspace_id')")
    rename = _index_of(calls, "alter_column('workspace_files', 'workspace_fk'")
    new_fk = _index_of(calls, "create_foreign_key('fk_workspace_files_workspace_id'")
    unique_index_drop = _index_of(calls, "drop_index('ix_workspaces_workspace_id'")
    plain_index = _index_of(calls, "create_index('ix_workspaces_workspace_id', 'workspaces', ['workspace_id'])")
    composite = _index_of(calls, "create_unique_constraint('uix_workspace_partition_id'")

    # The FK onto workspaces.workspace_id is what pins its unique index.
    assert fk_drop < backfill < old_column_drop < rename < new_fk < unique_index_drop < plain_index < composite
    assert "['partition_name', 'workspace_id']" in calls[composite]
    assert "['id']" in calls[new_fk]


def test_upgrade_is_a_no_op_on_the_target_shape(monkeypatch, migration) -> None:
    assert _run(monkeypatch, migration, migration.upgrade, _migrated) == []


def test_upgrade_also_drops_a_unique_constraint_variant(monkeypatch, migration) -> None:
    # ``Column(unique=True)`` without ``index=True`` materialises as a
    # constraint rather than a unique index; both spellings must go.
    def state(op):
        return {**_migrated(op), "unique_constraints_on": lambda *_: ["workspaces_workspace_id_key"]}

    calls = _run(monkeypatch, migration, migration.upgrade, state)
    assert calls == ["drop_constraint('workspaces_workspace_id_key', 'workspaces'){'type_': 'unique'}"]


def test_upgrade_logs_rows_that_reference_no_workspace(monkeypatch, migration, caplog) -> None:
    with caplog.at_level("WARNING", logger="alembic.runtime.migration"):
        _run(monkeypatch, migration, migration.upgrade, _legacy, dangling=3)
    assert [r.getMessage() for r in caplog.records] == ["workspace_files: removed 3 row(s) referencing no workspace"]


def test_upgrade_is_silent_when_every_row_resolves(monkeypatch, migration, caplog) -> None:
    with caplog.at_level("WARNING", logger="alembic.runtime.migration"):
        _run(monkeypatch, migration, migration.upgrade, _legacy, dangling=0)
    assert caplog.records == []


def test_downgrade_refuses_while_an_id_is_shared_across_partitions(monkeypatch, migration) -> None:
    with pytest.raises(RuntimeError, match=r"\['default', 'shared'\]"):
        _run(monkeypatch, migration, migration.downgrade, _migrated, duplicates=["shared", "default"])


def test_downgrade_restores_the_global_unique_index_before_the_string_fk(monkeypatch, migration) -> None:
    calls = _run(monkeypatch, migration, migration.downgrade, _migrated)

    composite_drop = _index_of(calls, "drop_constraint('uix_workspace_partition_id'")
    unique_index = _index_of(
        calls, "create_index('ix_workspaces_workspace_id', 'workspaces', ['workspace_id']){'unique': True}"
    )
    backfill = _index_of(calls, "SET workspace_str = w.workspace_id")
    string_fk = _index_of(calls, "create_foreign_key('fk_workspace_files_workspace_id'")

    assert composite_drop < unique_index < backfill < string_fk
    assert "['workspace_id']" in calls[string_fk]


def test_downgrade_is_a_no_op_on_the_legacy_shape(monkeypatch, migration) -> None:
    calls = _run(monkeypatch, migration, migration.downgrade, _legacy)
    # Only the duplicate check runs; nothing is altered.
    assert [call for call in calls if not call.startswith("SELECT")] == []
