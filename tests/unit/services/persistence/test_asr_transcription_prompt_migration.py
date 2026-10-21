"""Tests for the prompt-type migration used by managed ASR transcription."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.f6a7b8c9d0e1_add_asr_transcription_prompt",
    )


class _FakeOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_bind(self):
        return object()

    def drop_constraint(self, *args, **kwargs) -> None:
        self.calls.append(("drop_constraint", args, kwargs))

    def create_check_constraint(self, *args, **kwargs) -> None:
        self.calls.append(("create_check_constraint", args, kwargs))

    def execute(self, statement) -> None:
        self.calls.append(("execute", (str(statement),), {}))


def _set_constraint(monkeypatch: pytest.MonkeyPatch, migration, sql: str | None) -> None:
    constraints = [] if sql is None else [{"name": "ck_prompt_type", "sqltext": sql}]
    monkeypatch.setattr(
        migration,
        "inspect",
        lambda _bind: SimpleNamespace(get_check_constraints=lambda _table: constraints),
    )


def test_upgrade_replaces_the_prompt_type_constraint(monkeypatch: pytest.MonkeyPatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    _set_constraint(monkeypatch, migration, migration._PREVIOUS_PROMPT_TYPE_IN)

    migration.upgrade()

    assert fake_op.calls == [
        ("drop_constraint", ("ck_prompt_type", "prompts"), {"type_": "check"}),
        ("create_check_constraint", ("ck_prompt_type", "prompts", migration._PROMPT_TYPE_IN), {}),
    ]


def test_upgrade_keeps_an_already_updated_constraint(monkeypatch: pytest.MonkeyPatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    _set_constraint(monkeypatch, migration, migration._PROMPT_TYPE_IN)

    migration.upgrade()

    assert fake_op.calls == []


def test_downgrade_removes_asr_rows_and_restores_the_old_constraint(monkeypatch: pytest.MonkeyPatch, migration) -> None:
    fake_op = _FakeOp()
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    _set_constraint(monkeypatch, migration, migration._PROMPT_TYPE_IN)

    migration.downgrade()

    assert "DELETE FROM prompts WHERE prompt_type = 'asr_transcription'" in fake_op.calls[0][1][0]
    assert fake_op.calls[1:] == [
        ("drop_constraint", ("ck_prompt_type", "prompts"), {"type_": "check"}),
        ("create_check_constraint", ("ck_prompt_type", "prompts", migration._PREVIOUS_PROMPT_TYPE_IN), {}),
    ]
