"""Tests for the migration moving a stored 0.6 similarity threshold to 0.2."""

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
        "services.persistence.migrations.alembic.versions.4d7e2b9c1f35_lower_default_similarity_threshold",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement) -> None:
        self.statements.append(" ".join(str(statement).split()))


def test_revision_chain(migration) -> None:
    assert migration.down_revision == "f3a4b5c6d7e8"


def test_upgrade_only_rewrites_an_exact_06_on_retrieval_presets(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _t: True)
    monkeypatch.setattr(migration, "column_exists", lambda _t, _c: True)

    migration.upgrade()

    (sql,) = fake_op.statements
    assert "SET config = jsonb_set(config, '{similarity_threshold}', '0.2'::jsonb)" in sql
    assert "preset_type = 'retrieval'" in sql
    assert "config -> 'similarity_threshold' = '0.6'::jsonb" in sql


@pytest.mark.parametrize(
    ("has_table", "has_column"),
    [(False, False), (True, False)],
    ids=["no-table", "no-config-column"],
)
def test_upgrade_skips_when_table_or_column_is_missing(monkeypatch, migration, has_table, has_column) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _t: has_table)
    monkeypatch.setattr(migration, "column_exists", lambda _t, _c: has_column)

    migration.upgrade()

    assert fake_op.statements == []


def test_downgrade_is_a_noop(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.downgrade()

    assert fake_op.statements == []
