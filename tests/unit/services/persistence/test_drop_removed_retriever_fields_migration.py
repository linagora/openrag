"""Tests for the migration dropping the removed retrievers' fields from retrieval presets."""

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
        "services.persistence.migrations.alembic.versions.d9e2e59a7a4e_drop_removed_retriever_fields",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement) -> None:
        self.statements.append(" ".join(str(statement).split()))


def test_revision_chain(migration) -> None:
    assert migration.down_revision == "4d7e2b9c1f35"


def test_upgrade_drops_the_four_fields_from_retrieval_presets_only(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _t: True)
    monkeypatch.setattr(migration, "column_exists", lambda _t, _c: True)

    migration.upgrade()

    (sql,) = fake_op.statements
    removed = "'{type,llm,hyde_prompt_name,multi_query_prompt_name}'::text[]"
    assert f"SET config = config - {removed}" in sql
    assert "preset_type = 'retrieval'" in sql
    assert f"config <> config - {removed}" in sql


def test_upgrade_skips_when_table_or_column_is_missing(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _t: False)
    monkeypatch.setattr(migration, "column_exists", lambda _t, _c: False)

    migration.upgrade()

    assert fake_op.statements == []


def test_downgrade_is_a_noop(monkeypatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)

    migration.downgrade()

    assert fake_op.statements == []
