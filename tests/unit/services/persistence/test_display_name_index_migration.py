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
        "services.persistence.migrations.alembic.versions.e5f6a7b8c9d0_add_user_display_name_prefix_index",
    )


class _FakeContext:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    @contextmanager
    def autocommit_block(self):
        self.calls.append("autocommit")
        yield


class _FakeOp:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.context = _FakeContext(self.calls)

    def get_context(self):
        return self.context

    def execute(self, statement) -> None:
        self.calls.append(str(statement))


def test_upgrade_rebuilds_an_invalid_concurrent_index(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "_index_validity", lambda: False)

    migration.upgrade()

    statements = "\n".join(fake_op.calls)
    assert "DROP INDEX CONCURRENTLY ix_users_lower_display_name_pattern" in statements
    assert "CREATE INDEX CONCURRENTLY ix_users_lower_display_name_pattern" in statements


def test_upgrade_keeps_a_valid_index(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "_index_validity", lambda: True)

    migration.upgrade()

    assert fake_op.calls == []


def test_upgrade_creates_a_missing_index(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(migration, "_index_validity", lambda: None)

    migration.upgrade()

    statements = "\n".join(fake_op.calls)
    assert "DROP INDEX" not in statements
    assert "CREATE INDEX CONCURRENTLY ix_users_lower_display_name_pattern" in statements
