"""Tests for the migration adding the ``agent`` prompt type, and for the prompt types the table allows."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.models.prompt import PromptType
from services.persistence import schema


@pytest.fixture
def migration(monkeypatch):
    alembic_dir = (
        Path(__file__).resolve().parents[4] / "openrag" / "services" / "persistence" / "migrations" / "alembic"
    )
    monkeypatch.syspath_prepend(str(alembic_dir))
    return importlib.import_module(
        "services.persistence.migrations.alembic.versions.c9d0e1f2a3b4_add_agent_prompt",
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


def _as_postgres_shows_it(types: list[str]) -> str:
    """The constraint as Postgres gives it back, not as it was written."""
    values = ", ".join(f"'{t}'::character varying" for t in types)
    return f"(prompt_type)::text = ANY ((ARRAY[{values}])::text[])"


_BEFORE = [
    "sys_prompt",
    "query_contextualizer",
    "chunk_contextualizer",
    "image_captioning",
    "hyde",
    "multi_query",
    "spoken_style_answer",
    "topic_tagger",
    "asr_transcription",
]


def _apply(monkeypatch: pytest.MonkeyPatch, migration, constraint_sql: str | None) -> _FakeOp:
    fake_op = _FakeOp()
    constraints = [] if constraint_sql is None else [{"name": "ck_prompt_type", "sqltext": constraint_sql}]
    monkeypatch.setattr(migration, "op", fake_op)
    monkeypatch.setattr(migration, "table_exists", lambda _table: True)
    monkeypatch.setattr(
        migration, "inspect", lambda _bind: SimpleNamespace(get_check_constraints=lambda _table: constraints)
    )
    return fake_op


def test_upgrade_adds_agent_to_the_prompt_types(monkeypatch: pytest.MonkeyPatch, migration) -> None:
    fake_op = _apply(monkeypatch, migration, _as_postgres_shows_it(_BEFORE))

    migration.upgrade()

    assert fake_op.calls == [
        ("drop_constraint", ("ck_prompt_type", "prompts"), {"type_": "check"}),
        ("create_check_constraint", ("ck_prompt_type", "prompts", migration._PROMPT_TYPE_IN), {}),
    ]


def test_upgrade_keeps_a_constraint_that_has_agent_already(monkeypatch: pytest.MonkeyPatch, migration) -> None:
    fake_op = _apply(monkeypatch, migration, _as_postgres_shows_it([*_BEFORE, "agent"]))

    migration.upgrade()

    assert fake_op.calls == []


def test_downgrade_removes_agent_prompts_and_restores_the_old_constraint(
    monkeypatch: pytest.MonkeyPatch, migration
) -> None:
    fake_op = _apply(monkeypatch, migration, _as_postgres_shows_it([*_BEFORE, "agent"]))

    migration.downgrade()

    assert "DELETE FROM prompts WHERE prompt_type = 'agent'" in fake_op.calls[0][1][0]
    assert fake_op.calls[1:] == [
        ("drop_constraint", ("ck_prompt_type", "prompts"), {"type_": "check"}),
        ("create_check_constraint", ("ck_prompt_type", "prompts", migration._PREVIOUS_PROMPT_TYPE_IN), {}),
    ]


def test_every_prompt_type_can_be_stored(migration) -> None:
    # A type missing from the constraint fails seeding at startup, and the API serves 503.
    assert {t.value for t in PromptType} <= set(schema._PROMPT_TYPE_VALUES)
    assert migration._PROMPT_TYPE_IN == schema._PROMPT_TYPE_IN, "a fresh database and a migrated one agree"
